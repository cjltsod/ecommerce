# coding=utf-8
import logging

from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View, TemplateView
from oscar.apps.payment.exceptions import PaymentError, GatewayError
from oscar.core.loading import get_class, get_model

from spgateway_core.utils import decrypt_info, validate_info
from ecommerce.extensions.checkout.mixins import EdxOrderPlacementMixin
from ecommerce.extensions.checkout.utils import get_receipt_page_url
from ecommerce.extensions.payment.processors.spgateway import Spgateway

logger = logging.getLogger(__name__)

Applicator = get_class('offer.applicator', 'Applicator')
NoShippingRequired = get_class('shipping.methods', 'NoShippingRequired')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
PaymentProcessorResponse = get_model('payment', 'PaymentProcessorResponse')

from urlparse import parse_qs
import json


class SpgatewayMixin(EdxOrderPlacementMixin):
    @method_decorator(csrf_exempt)
    def dispatch(self, request, *args, **kwargs):
        return super(SpgatewayMixin, self).dispatch(request, *args, **kwargs)

    @property
    def payment_processor(self):
        return Spgateway(self.request.site)

    def _get_basket(self, payment_id, ignored_multiple_exception=False):
        return self.payment_processor.get_basket(
            payment_id,
            self.request,
            ignored_multiple_exception=ignored_multiple_exception,
        )

    def _get_payment_id(self, spgateway_response):
        return self.payment_processor.get_payment_id(spgateway_response)

    def _decrypt_spgateway_response(self, response_dict):
        hash_key, hash_iv = self.payment_processor.hash_key, self.payment_processor.hash_iv
        decrypted_trade_info_str = decrypt_info(hash_key, hash_iv, response_dict['TradeInfo'])
        trade_sha = response_dict['TradeSha']

        if not validate_info(hash_key, hash_iv, decrypted_trade_info_str, trade_sha):
            raise ValueError('Validate response failed')

        use_json = True
        if use_json:
            decrypted = json.loads(decrypted_trade_info_str)
        else:
            decrypted = parse_qs(decrypted_trade_info_str)

        return dict(
            Status=response_dict['Status'],
            MerchantID=response_dict['MerchantID'],
            TradeInfo=decrypted,
            TradeSha=trade_sha,
            Version=response_dict['Version'],
        )

    def get_receipt_url(self, request=None, payment_id=None, basket=None):
        if basket is None:
            if payment_id is None and request is not None:
                spgateway_encrypted_response = request.POST.dict()
                spgateway_response = self._decrypt_spgateway_response(spgateway_encrypted_response)
                payment_id = self._get_payment_id(spgateway_response)
            else:
                raise ValueError('At least offer one of request, payment_id or basket')
            basket = self._get_basket(payment_id, ignored_multiple_exception=True)
        if basket is not None:
            return get_receipt_page_url(
                order_number=basket.order_number,
                site_configuration=basket.site.siteconfiguration
            )
        else:
            raise ValueError('Basket not found')

    def _process_notify(self, request):
        spgateway_encrypted_response = request.POST.dict()
        spgateway_response = self._decrypt_spgateway_response(spgateway_encrypted_response)

        if spgateway_response['Status'] != 'SUCCESS':
            raise GatewayError(
                u'{}: {}'.format(spgateway_response['Status'], spgateway_response['TradeInfo']['Message']))

        payment_id = self._get_payment_id(spgateway_response)
        basket = self._get_basket(payment_id)

        if not basket:
            logger.exception('Attempts to find basket by payment_id [%s] failed.', payment_id)
            raise PaymentError('Unable to find basket from gateway response')

        # 請款作業
        try:
            with transaction.atomic():
                self.handle_payment(spgateway_response, basket)
        except:  # pylint: disable=bare-except
            logger.exception('Attempts to handle payment for basket [%d] failed.', basket.id)
            raise

        # 建立訂單
        try:
            # should this part inside handle_payment?!
            shipping_method = NoShippingRequired()
            shipping_charge = shipping_method.calculate(basket)
            order_total = OrderTotalCalculator().calculate(basket, shipping_charge)

            user = basket.owner
            # Given a basket, order number generation is idempotent. Although we've already
            # generated this order number once before, it's faster to generate it again
            # than to retrieve an invoice number from PayPal.
            order_number = basket.order_number

            order = self.handle_order_placement(
                order_number=order_number,
                user=user,
                basket=basket,
                shipping_address=None,
                shipping_method=shipping_method,
                shipping_charge=shipping_charge,
                billing_address=None,
                order_total=order_total,
                request=request,
            )
        except:  # pylint: disable=bare-except
            logger.exception(self.order_placement_failure_msg, basket.id)
            raise
        return spgateway_response, basket, order

    def _process_return(self, request):
        spgateway_encrypted_response = request.POST.dict()
        spgateway_response = self._decrypt_spgateway_response(spgateway_encrypted_response)

        if spgateway_response['Status'] != 'SUCCESS':
            raise GatewayError('{}: {}'.format(spgateway_response['Status'], spgateway_response))

        payment_id = self._get_payment_id(spgateway_response)
        basket = self._get_basket(payment_id, ignored_multiple_exception=True)

        if not basket:
            logger.exception('Attempts to find basket by payment_id [%s] failed.', payment_id)
            raise PaymentError('Unable to find basket from gateway response')

        return spgateway_response, basket


class SpgatewayReturnView(SpgatewayMixin, View):
    def post(self, request):
        try:
            if self._process_return(request):
                return redirect(self.get_receipt_url(request))
            else:
                return redirect(self.payment_processor.error_url)
        except:
            return redirect(self.payment_processor.error_url)


class SpgatewayNotifyView(SpgatewayMixin, View):
    def post(self, request):
        try:
            if self._process_notify(request):
                return JsonResponse(dict(success=True))
            else:
                return JsonResponse(dict(success=False))
        except:
            return JsonResponse(dict(success=False))


class SpgatewayNotifyReturnView(SpgatewayMixin, View):
    def post(self, request):
        try:
            if self._process_notify(request):
                return redirect(self.get_receipt_url(request))
            else:
                return redirect(self.payment_processor.error_url)
        except:
            raise
            return redirect(self.payment_processor.error_url)


class SpgatewayCustomerView(SpgatewayMixin, TemplateView):
    template_name = r'spgateway/customer.html'

    def post(self, request, *args, **kwargs):
        result, basket = self._process_return(request)
        context = self.get_context_data(**kwargs)
        context['spgateway_result'] = result
        context['basket'] = basket
        return self.render_to_response(context)
