import base64
from datetime import datetime
import logging
import os
from urllib.parse import parse_qs
from urllib.parse import urlencode
from urllib.parse import urlparse

from idpyoidc.message.oauth2 import AuthorizationErrorResponse
from idpyoidc.message.oauth2 import AuthorizationResponse
from idpyoidc.message.oauth2 import ResponseMessage
from idpyoidc.node import topmost_unit
from idpyoidc.server import Endpoint
from idpyoidc.server.authn_event import create_authn_event
from openid4v.message import AuthorizationRequest
import satosa
from satosa_idpyop.core import ExtendedContext
from satosa_idpyop.core.claims import combine_claim_values
from satosa_idpyop.core.response import JsonResponse
from satosa_idpyop.endpoint_wrapper import EndPointWrapper
from satosa_idpyop.endpoint_wrapper.authorization import handle_authorization_details_decoding
from satosa_idpyop.utils import get_http_info

logger = logging.getLogger(__name__)

try:
    from satosa.context import add_prompt_to_context
except ImportError:
    # TODO: remove after https://github.com/IdentityPython/SATOSA/pull/419 is merged
    def add_prompt_to_context(*args, **kwargs):
        pass
from satosa.internal import InternalData
import satosa.logging_util as lu
from satosa.response import SeeOther

logger = logging.getLogger(__name__)


class AuthorizationEndpointWrapper(EndPointWrapper):
    name = "authorization"
    msg_type = AuthorizationRequest
    response_cls = AuthorizationResponse
    error_msg = AuthorizationErrorResponse
    wraps = ['authorization']

    def __init__(self, upstream_get, endpoint, **kwargs):  # pragma: no cover
        EndPointWrapper.__init__(self, upstream_get, endpoint, **kwargs)
        self.auth_req_callback_func = kwargs.get("auth_req_callback_func", None)
        self.converter = kwargs.get("converter", None)
        # self.entity_type = app.server["openid_credential_issuer"]

    def __call__(self, context: ExtendedContext):
        """
        OAuth2 / OIDC Authorization endpoint
        Checks client_id and handles the authorization request
        """
        self.log_request(context, "Authorization endpoint request")
        self.load_cdb(context)

        get_http_info(context)
        internal_req = self.handle_authn_request(context, self.endpoint)
        if not isinstance(internal_req, InternalData):  # pragma: no cover
            return self.send_response(internal_req)

        return self.auth_req_callback_func(context, internal_req)

    def get_entity_type(self):
        return self.upstream_get('unit')

    def send_response(self, response):
        _entity_type = self.get_entity_type()
        _entity_type.persistence.flush_session_manager()
        return response

    def _handle_authn_request(self, context: ExtendedContext, endpoint):
        """
        Parse and verify the authentication request into an internal request.
        :type context: satosa.context.Context
        :rtype: satosa.internal.InternalData

        :param context: the current context
        :return: the internal request
        """
        self.log_request(context, "OAuth2 Authorization request from client")
        logger.debug(f"{endpoint}")
        logger.debug(f"request at frontend: {context.request}")

        if "authorization_details" in context.request:
            handle_authorization_details_decoding(context.request)

        http_info = get_http_info(context)
        logger.debug(f"http_info: {http_info}")
        self.load_cdb(context)
        parse_req = self.parse_request(context.request, http_info)
        if isinstance(parse_req, AuthorizationErrorResponse):
            logger.debug(f"{context.request}, {parse_req._dict}")
            return self.send_response(JsonResponse(parse_req._dict))

        _entity_type = self.upstream_get("unit")
        _entity_type.persistence.restore_state(parse_req, http_info)

        _unit = topmost_unit(self)
        context.state[_unit.frontend_name] = {"oidc_request": context.request}

        client_id = parse_req.get("client_id")
        _client_conf = endpoint.upstream_get("context").cdb[client_id]
        client_name = _client_conf.get("client_name")
        subject_type = _client_conf.get("subject_type", "pairwise")
        if client_name:
            requester_name = [{"lang": "en", "text": client_name}]
        else:  # pragma: no cover
            requester_name = None

        internal_req = InternalData(
            subject_type=subject_type,
            requester=client_id,
            requester_name=requester_name,
        )

        _claims_supported = endpoint.upstream_get("context").get_preference("claims_supported")

        logger.debug(f"Claims supported: {_claims_supported}")

        if _claims_supported:
            internal_req.attributes = self.converter.to_internal_filter("openid", _claims_supported)

        context.internal_data = internal_req
        return internal_req

    def handle_authn_request(self, context: ExtendedContext, endpoint: Endpoint):
        """
        Handle an authentication request and pass it on to the backend.
        :type context: satosa.context.Context
        :rtype: satosa.response.SeeOther

        :param context: the current context
        :return: HTTP response to the client
        """
        internal_req = self._handle_authn_request(context, endpoint)
        if not isinstance(internal_req, InternalData):
            return self.send_response(internal_req)
        logger.debug(f"InternalData: {internal_req}")
        logger.debug(f"Context: {context}")
        return self.auth_req_callback_func(context, internal_req)

    def _handle_backend_response(self, context: ExtendedContext, internal_resp):
        """
        Called by handle_authn_response, once a backend made its work
        :type context: satosa.context.Context
        :rtype: satosa.response.Response

        :param context: the current context
        :param internal_resp: satosa internal data
        :type internal_resp: satosa.internal.InternalData
        :return: HTTP response to the client
        """
        http_info = get_http_info(context)
        oidc_req = context.state[self.name]["oidc_request"]
        endpoint = self.app.server.endpoint["authorization"]
        _entity_type = self.upstream_get("unit")
        _entity_type.persistence.restore_client_info(oidc_req["client_id"])

        # not using self._parse_request cause of "Missing required attribute 'response_type'"
        parse_req = AuthorizationRequest().from_urlencoded(urlencode(oidc_req))

        proc_req = self.process_request(context, parse_req, http_info)

        if isinstance(proc_req, JsonResponse):  # pragma: no cover
            return self.send_response(proc_req)

        client_id = parse_req["client_id"]
        sub = internal_resp.subject_id

        kwargs = {}
        _timestamp = internal_resp["auth_info"].get("timestamp", "")
        if _timestamp:
            if _timestamp.endswith("Z"):
                _timestamp = _timestamp[0:len(_timestamp) - 1] + "+00:00"
            kwargs["authn_time"] = datetime.fromisoformat(_timestamp).timestamp()

        authn_event = create_authn_event(
            uid=sub,
            salt=base64.b64encode(os.urandom(self.app.salt_size)).decode(),
            authn_info=internal_resp.auth_info.auth_class_ref,
            **kwargs
        )

        _ec = endpoint.upstream_get("context")
        _token_usage_rules = _ec.authn_broker.get_method_by_id("user")

        session_manager = _ec.session_manager
        client = self.app.storage.get_client_by_id(client_id)
        client_subject_type = client.get("subject_type", "public")
        _session_id = session_manager.create_session(
            authn_event=authn_event,
            auth_req=parse_req,
            user_id=sub,
            client_id=client_id,
            sub_type=client_subject_type,
            token_usage_rules=_token_usage_rules,
        )

        try:
            # _args is a dict that contains:
            #  - idpyoidc.message.oidc.AuthorizationResponse
            #  - session_id
            #  - cookie (only need for logout -> not yet supported by Satosa)
            _args = endpoint.authz_part2(
                user=sub,
                session_id=_session_id,
                request=parse_req,
                authn_event=authn_event,
            )
        except ValueError as excp:  # pragma: no cover
            # TODO - cover with unit test and add some satosa logging ...
            logger.exception('ValueError')
            return self.handle_error(excp=excp)
        except Exception as excp:  # pragma: no cover
            logger.exception('Unknown error')
            return self.handle_error(excp=excp)

        if isinstance(_args, ResponseMessage) and "error" in _args:  # pragma: no cover
            return self.send_response(JsonResponse(_args, status="403"))
        elif isinstance(
                _args.get("response_args"), AuthorizationErrorResponse
        ):  # pragma: no cover
            rargs = _args.get("response_args")
            logger.error(rargs)
            response = JsonResponse(rargs.to_json(), status="403")
            return self.send_response(response)

        info = endpoint.do_response(request=parse_req, **proc_req)
        info_response = info["response"]
        _response_placement = info.get(
            "response_placement", endpoint.response_placement
        )
        if _response_placement == "url":
            data = _args["response_args"].to_dict()
            url_components = urlparse(info_response)
            original_params = parse_qs(url_components.query)
            merged_params = {**original_params, **data}
            updated_query = urlencode(merged_params, doseq=True)
            redirect_url = url_components._replace(query=updated_query).geturl()
            logger.debug(f"Redirect to: {redirect_url}")
            resp = SeeOther(redirect_url)
        else:  # pragma: no cover
            _entity_type = self.upstream_get("unit")
            _entity_type.persistence.flush_session_manager()
            raise NotImplementedError()

        # I don't flush in-mem db because it will be flushed by handle_authn_response
        return resp

    def handle_authn_response(self, context: ExtendedContext, internal_resp):
        """
        See super class method satosa.frontends.base.FrontendModule#handle_authn_response
        :type context: satosa.context.Context
        :type internal_resp: satosa.internal.InternalData
        :rtype satosa.response.SeeOther
        """
        _claims = self.converter.from_internal("openid", internal_resp.attributes)
        claims = {k: v for k, v in _claims.items() if v}
        combined_claims = dict([i for i in combine_claim_values(claims.items())])

        response = self._handle_backend_response(context, internal_resp)
        # TODO - why should we have to delete it?
        # del context.state[self.name]

        # store oidc session with user claims
        _entity_type = self.upstream_get("unit")
        _entity_type.persistence.store_state(claims=combined_claims)
        return self.send_response(response)

    def handle_backend_error(self, exception: Exception):
        """
        See super class satosa.frontends.base.FrontendModule
        :type exception: satosa.exception.SATOSAError
        :rtype: oic.utils.http_util.Response
        """
        auth_req = AuthorizationRequest().from_urlencoded(
            urlencode(exception.state[self.name]["oidc_request"])
        )
        msg = exception.message
        error_resp = AuthorizationErrorResponse(
            error="access_denied",
            error_description=msg,
            # If the client sent us a state parameter, we should reflect it back according to the
            # spec
            **({"state": auth_req["state"]} if "state" in auth_req else {}),
        )
        logline = lu.LOG_FMT.format(
            id=lu.get_session_id(exception.state), message=msg)
        logger.info(logline)
        return SeeOther(
            error_resp.request(
                auth_req["redirect_uri"], auth_req["response_type"] != ["code"]
            )
        )
