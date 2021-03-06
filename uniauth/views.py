import base64
import datetime
import copy
import logging

from django.conf import settings
from django.contrib.auth import logout, login as auth_login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView
from django.core.exceptions import (ImproperlyConfigured,
                                    PermissionDenied,
                                    SuspiciousOperation)
from django.http import (HttpResponse,
                         HttpResponseBadRequest,
                         HttpResponseRedirect)
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.datastructures import MultiValueDictKeyError
from django.utils.decorators import method_decorator
from django.utils.module_loading import import_string
from django.utils.translation import gettext as _
from django.utils import timezone
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.views.decorators.cache import never_cache
from django.shortcuts import render_to_response
from saml2 import BINDING_HTTP_POST, BINDING_HTTP_REDIRECT
from saml2.authn_context import (PASSWORD,
                                 AuthnBroker,
                                 authn_context_class_ref)

from saml2.assertion import Policy
from saml2.config import IdPConfig
from saml2.ident import NameID
from saml2.metadata import entity_descriptor
from saml2.s_utils import (UnknownPrincipal,
                           UnsupportedBinding,
                           UnknownSystemEntity)
from saml2.saml import NAMEID_FORMAT_UNSPECIFIED
from saml2.response import (IncorrectlySigned,)
from six import text_type

from . decorators import (_not_valid_saml_msg,
                          store_params_in_session_func,
                          require_saml_request)
from . exceptions import MetadataNotFound, MetadataCorruption
from . forms import AgreementForm, LoginForm
from . models import AgreementRecord, ServiceProvider
from . processors import BaseProcessor
from . utils import (repr_saml,
                     get_idp_config,
                     get_idp_sp_config,
                     get_client_id)


# already registered into decorators
logger = logging.getLogger(__name__)


@never_cache
@require_http_methods(["GET", "POST"])
@csrf_exempt
@store_params_in_session_func
def sso_entry(request, binding):
    """ Entrypoint view for SSO. Build the saml session and redirects
        the requester to the login_process view.
    """
    # decoratos do the most
    logger.info("SSO req from client {}".format(get_client_id(request)))
    return HttpResponseRedirect(reverse('uniauth:saml_login_process'))


class ErrorHandler(object):
    error_view = import_string(getattr(settings,
                                       'SAML_IDP_ERROR_VIEW_CLASS',
                                       'uniauth.error_views.SamlIDPErrorView'))

    def handle_error(self, request, **kwargs):
        logger.error(kwargs)
        return self.error_view.as_view()(request, **kwargs)


class IdPHandlerViewMixin(ErrorHandler):
    """ Contains some methods used by multiple views
    """

    def dispatch(self, request, *args, **kwargs):
        """ Construct IDP server with config from settings dict
        """
        try:
            self.IDP = get_idp_config(settings.SAML_IDP_CONFIG)
        except Exception as e:
            return self.handle_error(request, exception=e)
        return super().dispatch(request, *args, **kwargs)

    def set_sp(self, sp_entity_id):
        """ Saves SP info to instance variable
            Raises an exception if sp matching
            the given entity id cannot be found.
        """
        self.sp = {'id': sp_entity_id}
        try:
            self.sp['config'] = get_idp_sp_config()[sp_entity_id]
        except KeyError:
            msg = _("No config for SP {} was defined in SAML_IDP_SPCONFIG").format(sp_entity_id)
            raise ImproperlyConfigured(msg)

    def set_processor(self):
        """ Instantiate user-specified processor or
            default to an all-access base processor.
            Raises an exception if the configured processor
            class can not be found or initialized.
        """
        processor_string = self.sp['config'].get('processor', None)
        if processor_string:
            try:
                self.processor = import_string(processor_string)(self.sp['id'])
                return
            except Exception as e:
                msg = _("Failed to instantiate processor: {} - {}")
                logger.error(msg.format(processor_string,e),
                                        exc_info=True)
                raise ImproperlyConfigured(_(msg.format(processor_string, e),
                                                        exc_info=True))
        self.processor = BaseProcessor(self.sp['id'])

    def verify_request_signature(self, req_info):
        """ Signature verification
            for authn request signature_check is at
            saml2.sigver.SecurityContext.correctly_signed_authn_request
        """
        # TODO: Add unit tests for this
        if not req_info.signature_check(req_info.xmlstr):
            raise ValueError(_("Message signature verification failure"))

    def check_access(self, request):
        """ Check if user has access to the service of this SP
        """
        if not self.processor.has_access(request):
            raise PermissionDenied(_("You do not have access to this resource"))

    def get_authn(self, req_info=None):
        if req_info:
            req_authn_context = req_info.message.requested_authn_context
        else:
             req_authn_context = PASSWORD
        broker = AuthnBroker()
        broker.add(authn_context_class_ref(req_authn_context), "")
        return broker.get_authn_by_accr(req_authn_context)

    def build_authn_response(self, user, authn, resp_args):
        """ pysaml2 server.Server.create_authn_response wrapper
        """
        self.sp['name_id_format'] = resp_args.get('name_id_policy').format
        idp_name_id_format_list = self.IDP.config.getattr("name_id_format",
                                                          "idp")

        # name_id format availability
        if idp_name_id_format_list and not self.sp['name_id_format']:
            name_id_format = idp_name_id_format_list[0]

        elif self.sp['name_id_format'] and not idp_name_id_format_list:
            name_id_format = self.sp['name_id_format']

        elif self.sp['name_id_format'] not in idp_name_id_format_list:
            return self.handle_error(request,
                                     exception=_('SP requested a name_id_format '
                                                 'that is not supported in the IDP'))
        elif self.sp['name_id_format'] in idp_name_id_format_list:
            name_id_format = self.sp['name_id_format']

        else:
            name_id_format = NAMEID_FORMAT_UNSPECIFIED

        # if SP doesn't request a specific name_id_format...
        if not self.sp['name_id_format']:
            self.sp['name_id_format'] = name_id_format

        user_id = self.processor.get_user_id(user, self.sp, self.IDP.config)
        name_id = NameID(format=name_id_format,
                         sp_name_qualifier=self.sp['id'],
                         text=user_id)

        #user_attrs = self.processor.create_identity(user, self.sp)

        # Generate request session stuff needed for user agreement screen
        attrs_to_exclude = self.sp['config'].get('user_agreement_attr_exclude', []) + \
                           getattr(settings, "SAML_IDP_USER_AGREEMENT_ATTR_EXCLUDE", [])

        self.request.session['identity'] = {
            k: v
            for k, v in self.processor.create_identity(self.request.user,
                                                       self.sp).items()
            if k not in attrs_to_exclude
        }


        # ASSERTION ENCRYPTED
        encrypt_response = getattr(settings,
                                   'SAML_ENCRYPT_AUTHN_RESPONSE',
                                   False)
        if 'encrypt_saml_responses' in self.sp['config'].keys():
            encrypt_response = self.sp['config'].get('encrypt_saml_responses')

        encrypt_advice_attributes = getattr(settings,
                                            'SAML_ENCRYPT_ADV_ATTRIBUTES',
                                             False)
        if 'encrypt_advice_attributes' in self.sp['config'].keys():
            encrypt_advice_attributes = self.sp['config'].get('encrypt_advice_attributes')

        authn_resp = self.IDP.create_authn_response(
            authn=authn,
            identity=self.request.session['identity'],
            userid=user_id,
            name_id=name_id,

            # signature
            sign_response=self.sp['config'].get("sign_response") or \
                          self.IDP.config.getattr("sign_response", "idp") or \
                          False,
            sign_assertion=self.sp['config'].get("sign_assertion") or \
                           self.IDP.config.getattr("sign_assertion", "idp") or \
                           False,

            # default will be sha1 in pySAML2
            sign_alg=self.sp['config'].get("signing_algorithm") or \
                     getattr(settings, 'SAML_AUTHN_SIGN_ALG', False),
            digest_alg=self.sp['config'].get("digest_algorithm") or \
                       getattr(settings, 'SAML_AUTHN_DIGEST_ALG', False),

            # Encryption
            encrypt_assertion=encrypt_response,
            encrypt_advice_attributes=encrypt_advice_attributes,
            **resp_args
        )

        # entity categories and other pysaml2 policies could filter out some attributes
        policy = Policy(restrictions=settings.SAML_IDP_CONFIG['service']['idp'].get('policy'))
        ava = policy.filter(self.request.session['identity'],
                            self.sp['id'],
                            self.IDP.config.metadata,
                            required=[])

        # talking logs
        self.request.session['authn_log'] = ('SSO AuthnResponse to {} [{}]:'
                                             ' {} attrs ({}) on {} '
                                             'filtered by policy').format(self.sp['id'],
                                                                          self.request.session.get('message_id'),
                                                                          len(ava),
                                                                          ','.join(ava.keys()),
                                                                          len(self.request.session['identity']))
        logger.info(self.request.session['authn_log'])
        #

        self.request.session['identity'] = ava

        return authn_resp

    def create_html_response(self, request, binding,
                             authn_resp, destination, relay_state):
        """ Login form for SSO
        """
        if binding == BINDING_HTTP_POST:
            context = {
                "acs_url": destination,
                "saml_response": base64.b64encode(authn_resp.encode()).decode(),
                "relay_state": relay_state,
            }
            template = "saml_post.html"
            html_response = render_to_string(template, context=context,
                                             request=request)
        else:
            http_args = self.IDP.apply_binding(
                binding=binding,
                msg_str=authn_resp,
                destination=destination,
                relay_state=relay_state,
                response=True)

            logger.debug('http args are: %s' % http_args)
            html_response = http_args['data']
        return html_response

    def render_response(self, request, html_response):
        """ Return either as redirect to MultiFactorView
            or as html with self-submitting form.
        """
        if not hasattr(self, 'processor'):
            # In case of SLO, where processor isn't relevant
            return HttpResponse(html_response)

        request.session['saml_data'] = html_response

        request.session['sp_display_info'] = {
            'display_name': self.sp['config'].get('display_name', self.sp['id']),
            'display_description': self.sp['config'].get('display_description'),
            'display_agreement_message': self.sp['config'].get('display_agreement_message'),
            'display_agreement_consent_form': self.sp['config'].get('display_agreement_consent_form')
            }
        request.session['sp_entity_id'] = self.sp['id']

        # Conditions for showing user agreement screen
        user_agreement_enabled_for_sp = self.sp['config'].get('show_user_agreement_screen',
                                                              getattr(settings,
                                                                      "SAML_IDP_SHOW_USER_AGREEMENT_SCREEN"))

        agreement_for_sp = AgreementRecord.objects.filter(user=request.user,
                                                          sp_entity_id=self.sp['id']).first()
        if agreement_for_sp:
            if agreement_for_sp.is_expired() or \
               agreement_for_sp.wants_more_attrs(request.session['identity'].keys()):
                agreement_for_sp.delete()
                already_agreed = False
            else:
                already_agreed = True
        else:
            already_agreed = False

        # Multifactor goes before user agreement because might result in user not being authenticated
        if self.processor.enable_multifactor(request.user):
            logger.debug("Redirecting to process_multi_factor")
            return HttpResponseRedirect(reverse('uniauth:saml_multi_factor'))

        # If we are here, there's no multifactor. Check whether to show user agreement
        if user_agreement_enabled_for_sp and not already_agreed:
            logger.debug("Redirecting to process_user_agreement")
            return HttpResponseRedirect(reverse('uniauth:saml_user_agreement'))

        # No multifactor or user agreement
        logger.debug("Performing SAML redirect")
        return HttpResponse(html_response)


@method_decorator(require_saml_request, name='dispatch')
class LoginAuthView(LoginView):
    """ First Login Form
    """
    template_name = "saml_login.html"
    form_class = LoginForm

    def dispatch(self, request, *args, **kwargs):
        """ Check if the SP is in metadata and have required attr mapping
        """
        binding = request.session.get('Binding', BINDING_HTTP_POST)

        # Check if SP is federated
        try:
            IDP = get_idp_config(settings.SAML_IDP_CONFIG)
        except MetadataNotFound as exp:
            return render_to_response('error.html',
                                      {'exception_type': _("Unable to find Service "
                                                           "Provider Metadata"),
                                       'exception_msg': "",
                                       'extra_message': _('SP Metadata are expired '
                                                          'or not found. Please contact '
                                                          'IDP technical support for '
                                                          'better acknowledge')},
                                       status=403)

        except MetadataCorruption as exp:
            logger.debug(exp)
            return render_to_response('error.html',
                                      {'exception_type': _("Some Metadata "
                                                           "seems to be corrupted"),
                                       'exception_msg': "",
                                       'extra_message': _('This is a security exception. '
                                                          'Please contact IdP staff.')},
                                       status=403)

        try:
            req_info = IDP.parse_authn_request(request.session['SAMLRequest'],
                                               binding)
            # later we'll check if the authnrequest is older then the IDP session age
            request.session['issue_instant'] = req_info.message.issue_instant
        except IncorrectlySigned as exp:
            return render_to_response('error.html',
                                      {'exception_type': exp,
                                       'exception_msg': _("Incorrectly signed"),
                                       'extra_message': _('SP Metadata '
                                                          'is changed, expired '
                                                          'or unavailable.')},
                                       status=403)
        except Exception as exp:
            return render_to_response('error.html',
                                      {'exception_type': exp},
                                       status=403)

        try:
            resp_args = IDP.response_args(req_info.message)
        except UnknownSystemEntity as exp:
            return render_to_response('error.html',
                                      {'exception_type': exp,
                                       'exception_msg': _("This SP is not federated"),
                                       'extra_message': _('Metadata is missing')},
                                       status=403)
        if resp_args.get('sp_entity_id') not in get_idp_sp_config().keys():
            return render_to_response('error.html',
                                      {'exception_type': _("This SP is not federated yet"),
                                       'exception_msg': _("Attribute Processor needs "
                                                          "to be configured ")},
                                      status=403)
        # end check
        return super().dispatch(request, *args, **kwargs)

    def form_invalid(self, form):
        """If the form is invalid, returns a generic message
        status code 200 to prevent brute force attack based to response code!
        """
        return render_to_response('error.html',
                                  {'exception_type':_("You cannot access to this service"),
                                   'exception_msg':_("Your Username or Password is invalid, "
                                                     "your account could be expired or been "
                                                     "disabled due to many login attempts."),
                                   'extra_message':_("Please access to 'Forgot your Password' "
                                                     "procedure, before contact the help desk.")},
                                  status=200)

    def form_valid(self, form):
        """Security check complete. Log the user in."""

        # check issue instant
        now = timezone.localtime()
        issue_instant = now
        for tformat in settings.SAML2_DATETIME_FORMATS:
            try:
                issue_instant = timezone.datetime.strptime(self.request.session['issue_instant'],
                                                           tformat)
                break
            except:
                # do not worry...
                pass
        # end check
        mins = getattr(settings, 'SESSION_COOKIE_AGE', 600)
        if issue_instant < timezone.make_naive((now-datetime.timedelta(minutes=mins)),
                                               timezone.get_current_timezone()):
            return render_to_response('error.html',
                                      {'exception_type': _("You take too long to authenticate!"),
                                       'exception_msg': _("Your request is expired"),
                                       'extra_message': _('{} minutes are passed').format(mins)},
                                       status=403)
        # end check issue instant

        user = form.get_user()
        auth_login(self.request, user)
        if self.request.POST.get('forget_agreement'):
            # TODO: also add the sp_nameid in the query?
            agr = AgreementRecord.objects.filter(user=self.request.user)
            agr.delete()

        if self.request.POST.get('forget_login'):
            self.request.session['forget_login'] = 1
        return HttpResponseRedirect(self.get_success_url())


@method_decorator(never_cache, name='dispatch')
class LoginProcessView(LoginRequiredMixin, IdPHandlerViewMixin, View):
    """ View which processes the actual SAML request and
        returns a self-submitting form with the SAML response.
        The login_required decorator ensures the user authenticates
        first on the IdP using 'normal' ways.
    """

    def get(self, request, *args, **kwargs):
        binding = request.session.get('Binding', BINDING_HTTP_POST)
        try:
            # Parse incoming request
            req_info = self.IDP.parse_authn_request(request.session['SAMLRequest'],
                                                    binding)
            # check SAML request signature
            self.verify_request_signature(req_info)

            # Compile Response Arguments
            self.resp_args = self.IDP.response_args(req_info.message)
            # Set SP and Processor
            self.set_sp(self.resp_args['sp_entity_id'])
            self.set_processor()
            # Check if user has access
            self.check_access(request)
            # Construct SamlResponse message
            self.authn_resp = self.build_authn_response(request.user,
                                                        self.get_authn(),
                                                        self.resp_args)
        except ValueError as excp:
            return self.handle_error(request, exception=excp, status=400)
        except (UnknownPrincipal, UnsupportedBinding) as excp:
            return self.handle_error(request, exception=excp, status=400)
        except ImproperlyConfigured as excp:
            return self.handle_error(request, exception=excp, status=500)
        except UnknownSystemEntity as excp:
            return self.handle_error(request,
                                     exception=excp,
                                     exception_msg=_('This SP needs attribute mappings'),
                                     status=403)
        except PermissionDenied as e:
            return self.handle_error(request, exception=e, status=403)
        except Exception as excp:
            return self.handle_error(request, exception=excp, status=500)

        html_response = self.create_html_response(
            request,
            binding=self.resp_args['binding'],
            authn_resp=self.authn_resp,
            destination=self.resp_args['destination'],
            relay_state=request.session['RelayState'])

        logger.debug("SAML Authn Response [\n{}]".format(repr_saml(self.authn_resp)))
        # already logged in build_auth_response
        # logger.info("SAML Authn Response to {} [{}] in response of: {}.".format(self.resp_args['sp_entity_id'],
                                                                               # self.resp_args['destination'],
                                                                               # self.resp_args['in_response_to']))

        return self.render_response(request, html_response)


@method_decorator(never_cache, name='dispatch')
class SSOInitView(LoginRequiredMixin, IdPHandlerViewMixin, View):
    """ View used for IDP initialized login,
        doesn't handle any SAML authn request
    """

    def post(self, request, *args, **kwargs):
        return self.get(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        passed_data = request.POST or request.GET

        try:
            # get sp information from the parameters
            self.set_sp(passed_data['sp'])
            self.set_processor()
            # Check if user has access to SP
            self.check_access(request)
        except (KeyError, ImproperlyConfigured) as excp:
            return self.handle_error(request, exception=excp, status=400)
        except PermissionDenied as excp:
            return self.handle_error(request, exception=excp, status=403)

        binding_out, destination = self.IDP.pick_binding(
            service="assertion_consumer_service",
            entity_id=self.sp['id'])

        # ##Adding a few things that would have been added
        # if this were SP Initiated
        passed_data['destination'] = destination
        passed_data['in_response_to'] = "IdP_Initiated_Login"
        passed_data['sp_entity_id'] = self.sp['id']

        # ##Construct SamlResponse messages
        try:
            authn_resp = self.build_authn_response(request.user,
                                                   self.get_authn(),
                                                   passed_data)
        except Exception as excp:
            return self.handle_error(request, exception=excp, status=500)

        html_response = self.create_html_response(request, binding_out,
                                                  authn_resp, destination,
                                                  passed_data['RelayState'])
        return self.render_response(request, html_response)


@method_decorator(never_cache, name='dispatch')
class UserAgreementScreen(ErrorHandler, LoginRequiredMixin, View):
    """This view shows the user an overview of the data being sent to the SP.
    """

    def get(self, request, *args, **kwargs):
        template = 'user_agreement.html'
        context = dict()
        try:
            # prevents KeyError at /login/process_user_agreement/: 'sp_display_info'
            context['sp_display_name'] = request.session['sp_display_info']['display_name']
            context['sp_display_description'] = request.session['sp_display_info']['display_description']
            context['sp_display_agreement_message'] = request.session['sp_display_info'].get('display_agreement_message')
            context['sp_display_agreement_consent_form'] = request.session['sp_display_info'].get('display_agreement_consent_form')
            context['attrs_passed_to_sp'] = request.session['identity']
        except Exception as excp:
            logout(request)
            logging.debug('{}'.format(excp))
            msg = _not_valid_saml_msg
            return self.handle_error(request, exception=excp,
                                     extra_message=msg)

        context['form'] = AgreementForm()
        html_response = render_to_string(template, context=context,
                                         request=request)
        return HttpResponse(html_response)

    def post(self, request, *args, **kwargs):
        form = AgreementForm(request.POST)
        if not form.is_valid():
            return render_to_response('error.html',
                                      {'exception_type':_("Invalid submission")},
                                      status=403)

        confirm = int(form.cleaned_data['confirm'])
        dont_show_again = form.cleaned_data['dont_show_again']

        if not confirm:
            logout(request)
            return render_to_response('error.html',
                                      {'exception_type':_("You cannot access to this service")},
                                      status=403)

        if dont_show_again:
            record = AgreementRecord(
                user=request.user,
                sp_entity_id=request.session['sp_entity_id'],
                attrs=",".join(request.session['identity'].keys())
            )
            record.save()

        saml_response_data = request.session.get('saml_data')
        if request.session.get('forget_login'):
            logout(request)
        return HttpResponse(saml_response_data)


@method_decorator(never_cache, name='dispatch')
class ProcessMultiFactorView(LoginRequiredMixin, View):
    """ This view is used in an optional step is to perform 'other'
        user validation, for example 2nd factor checks.
        Override this view per the documentation if using this
        functionality to plug in your custom validation logic.
    """

    def multifactor_is_valid(self, request):
        """ The code here can do whatever it needs to validate your
            user (via request.user or elsewise).
            It must return True for authentication
            to be considered a success.
        """
        return True

    def get(self, request, *args, **kwargs):
        if self.multifactor_is_valid(request):
            logger.debug('MultiFactor succeeded for %s' % request.user)

            # Check if user agreement redirect needed
            if request.session.get('sp_display_info'):
                # Arbitrary value that's only set if user agreement needed.
                return HttpResponseRedirect(reverse('uniauth:saml_user_agreement'))
            return HttpResponse(request.session['saml_data'])
        logger.debug(_("MultiFactor failed; %s will not be able to log in") % request.user)
        logout(request)
        raise PermissionDenied(_("MultiFactor authentication factor failed"))


@method_decorator(never_cache, name='dispatch')
@method_decorator(csrf_exempt, name='dispatch')
class LogoutProcessView(LoginRequiredMixin, IdPHandlerViewMixin, View):
    """ View which processes the actual SAML Single Logout request
        The login_required decorator ensures the user authenticates
        first on the IdP using 'normal' way.
    """
    __service_name = 'Single LogOut'

    def post(self, request, *args, **kwargs):
        return self.get(request, *args, **kwargs)

    @method_decorator(store_params_in_session_func)
    def get(self, request, *args, **kwargs):
        logger.debug("{} Service".format(self.__service_name))
        # do not assign a variable that overwrite request object
        # if it will fail the return with HttpResponseBadRequest trows naturally
        # store_params_in_session(request) -> now is a decorator
        binding = request.session['Binding']
        relay_state = request.session['RelayState']
        logger.debug("{} requested [\n{}] to IDP".format(self.__service_name, binding))

        # adapted from pysaml2 examples/idp2/idp_uwsgi.py
        try:
            req_info = self.IDP.parse_logout_request(request.session['SAMLRequest'], binding)
        except Exception as excp:
            expc_msg = "{} Bad request: {}".format(self.__service_name, excp)
            logger.error(expc_msg)
            return self.handle_error(request, exception=expc_msg, status=400)

        logger.info("{} from {} for [{}]".format(self.__service_name,
                                                 req_info.message.name_id.sp_name_qualifier,
                                                 req_info.message.name_id.text))
        logger.debug("{} SAML request [\n{}]".format(self.__service_name,
                                                             repr_saml(req_info.xmlstr, b64=False)))

        # TODO
        # check SAML request signature by hands?
        self.verify_request_signature(req_info)
        resp = self.IDP.create_logout_response(req_info.message, [binding])

        '''
        # TODO: SOAP
        # if binding == BINDING_SOAP:
            # destination = ""
            # response = False
        # else:
            # binding, destination = IDP.pick_binding(
                # "single_logout_service", [binding], "spsso", req_info
            # )
            # response = True
        # END TODO SOAP'''

        try:
            # hinfo returns request or response, it depends by request arg
            hinfo = self.IDP.apply_binding(binding, resp.__str__(),
                                           resp.destination,
                                           relay_state, response=True)
        except Exception as excp:
            logger.error("ServiceError: %s", excp)
            return self.handle_error(request, exception=excp, status=400)
            # return resp(self.environ, self.start_response)

        logger.debug("{} Response [\n{}]".format(self.__service_name,
                                                         repr_saml(resp.__str__().encode())))
        logger.debug("binding: {} destination:{} relay_state:{}".format(binding,
                                                                        resp.destination,
                                                                        relay_state))

        # TODO: double check username session and saml login request
        # logout user from IDP
        logout(request)

        if hinfo['method'] == 'GET':
            return HttpResponseRedirect(hinfo['headers'][0][1])
        else:
            html_response = self.create_html_response(
                request,
                binding=binding,
                authn_resp=resp.__str__(),
                destination=resp.destination,
                relay_state=relay_state)
        return self.render_response(request, html_response)


@never_cache
def get_metadata(request):
    if hasattr(settings, "SAML_IDP_MULTIFACTOR_VIEW"):
        multifactor_class = import_string(getattr(settings,
                                                  "SAML_IDP_MULTIFACTOR_VIEW"))
    else:
        multifactor_class = ProcessMultiFactorView
    return multifactor_class.as_view()(request)


@never_cache
def metadata(request):
    """ Returns an XML with the SAML 2.0 metadata for this Idp.
        The metadata is constructed on-the-fly based on the
        config dict in the django settings.
    """
    conf = IdPConfig()
    conf.load(copy.deepcopy(settings.SAML_IDP_CONFIG))
    metadata = entity_descriptor(conf)
    return HttpResponse(content=text_type(metadata).encode('utf-8'),
                        content_type="text/xml; charset=utf8")
