""" Spgateway payment processing. """
from __future__ import absolute_import, unicode_literals

import logging

from django.core.exceptions import MultipleObjectsReturned
from django.urls import reverse
from oscar.apps.partner import strategy
from oscar.apps.payment.exceptions import GatewayError
from oscar.core.loading import get_model, get_class

from spgateway_core import consts
from spgateway_core.credit_close import CreditCloseClient
from spgateway_core.mpg import generate_trade_info_dict, generate_trade_info_from_dict
from spgateway_core.utils import encrypt_info, generate_sha, generate_string
from ecommerce.core.url_utils import get_ecommerce_url
from ecommerce.extensions.payment.processors import (
    BasePaymentProcessor,
    HandledProcessorResponse
)
from ecommerce.extensions.payment.utils import middle_truncate

logger = logging.getLogger(__name__)

Applicator = get_class('offer.applicator', 'Applicator')
BillingAddress = get_model('order', 'BillingAddress')
Country = get_model('address', 'Country')
PaymentEvent = get_model('order', 'PaymentEvent')
PaymentEventType = get_model('order', 'PaymentEventType')
PaymentProcessorResponse = get_model('payment', 'PaymentProcessorResponse')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')


def generate_order_no(order_number):
    result_str = ''
    for each in order_number:
        if each.isalpha() or each.isdigit() or each == '_':
            result_str += each
        else:
            result_str += '_'
    return '{}_{}'.format(result_str, generate_string(consts.MAX_ORDER_LEN - len(result_str) - 1))


class Spgateway(BasePaymentProcessor):
    NAME = 'spgateway'

    @property
    def cancel_url(self):
        return get_ecommerce_url(self.configuration['cancel_checkout_path'])

    @property
    def error_url(self):
        return get_ecommerce_url(self.configuration['error_path'])

    def __init__(self, site):
        """
        Constructs a new instance of the Spgateway processor.

        Raises:
            KeyError: If no settings configured for this payment processor.
        """
        super(Spgateway, self).__init__(site)
        configuration = self.configuration
        self.merchant_id = configuration['MerchantID']
        self.hash_key = configuration['HashKey']
        self.hash_iv = configuration['HashIV']

    def get_payment_id(self, spgateway_response):
        return spgateway_response['TradeInfo']['Result']['MerchantOrderNo']

    def get_basket(self, payment_id, request=None, ignored_multiple_exception=False):
        """
        Retrieve a basket using a payment ID.

        Arguments:
            payment_id: MerchantOrderNo generated for Spgateway.
            request: Django request.
            ignored_multiple_exception: get basket without double pay check

        Returns:
            It will return related basket or log exception and return None if
            duplicate payment_id received or any other exception occurred.

        """
        try:
            try:
                basket = PaymentProcessorResponse.objects.get(
                    processor_name=self.NAME,
                    transaction_id=payment_id
                ).basket
            except MultipleObjectsReturned:
                if ignored_multiple_exception:
                    basket = PaymentProcessorResponse.objects.filter(
                        processor_name=self.NAME,
                        transaction_id=payment_id
                    ).first().basket
                else:
                    logger.warning(u"Duplicate payment ID [%s] received from Spgateway.", payment_id)
                    return None
            basket.strategy = strategy.Default()
            Applicator().apply(basket, basket.owner, request)
            return basket
        except Exception:  # pylint: disable=broad-except
            logger.exception(u"Unexpected error during basket retrieval while Spgateway payment.")
            raise
            return None

    def get_transaction_parameters(self, basket, request=None, use_client_side_checkout=False, **kwargs):
        return_url = get_ecommerce_url(reverse('spgateway:return'))
        notify_url = get_ecommerce_url(reverse('spgateway:notify'))

        # add the following replacement for localhost testing
        if notify_url.startswith('http://localhost:18130/'):
            notify_url = notify_url.replace('http://localhost:18130/', 'http://somewhere.elsenot.exist/')

        MerchantOrderNo = generate_order_no(basket.order_number)
        ItemDescList = list()
        for line in basket.all_lines():
            ItemDescList.append('{}x{}'.format(line.quantity, middle_truncate(line.product.title, 127)))
        item_desc = ', '.join(ItemDescList)

        if request.user.is_authenticated:
            email = request.user.email

        trade_info_dict = generate_trade_info_dict(
            MerchantID=self.merchant_id,
            MerchantOrderNo=MerchantOrderNo,
            Amt=basket.total_incl_tax,
            ItemDesc=item_desc,
            Email=email,
            ReturnURL=return_url,
            NotifyURL=notify_url,
            ClientBackURL=self.cancel_url,
            # # Disable all payment method cause editable from spgateway dashboard
            # CREDIT=True,
            # WEBATM=True,
            # VACC=True,
            # CVS=True,
            # BARCODE=True,
        )

        trade_info = generate_trade_info_from_dict(trade_info_dict)

        trade_info_encrypted = encrypt_info(
            hash_key=self.hash_key,
            hash_iv=self.hash_iv,
            info=trade_info,
        )

        trade_sha = generate_sha(
            hash_key=self.hash_key,
            hash_iv=self.hash_iv,
            encrypted_info=trade_info_encrypted,
        )

        parameters = {
            'MerchantID': self.merchant_id,
            'TradeInfo': trade_info_encrypted,
            'TradeSha': trade_sha,
            'Version': consts.MPG_VERSION,
            'payment_page_url': self.configuration['payment_page_url'],
        }

        # record for query when return from spgateway
        self.record_processor_response(
            trade_info_dict,
            transaction_id=MerchantOrderNo,
            basket=basket
        )

        return parameters

    def _credit_close_execute(self, **kwargs):
        credit_close_client = CreditCloseClient(
            merchant_id=self.merchant_id,
            hash_key=self.hash_key,
            hash_iv=self.hash_iv,
            credit_close_url=self.configuration.get('credit_close_url'),
        )
        return credit_close_client.execute(**kwargs)

    def handle_processor_response(self, response, basket=None):
        payment_id = response['TradeInfo']['Result']['MerchantOrderNo']
        payment_type = response['TradeInfo']['Result']['PaymentType']
        amt = response['TradeInfo']['Result'].get('Amt')

        if basket is None:
            basket = self.get_basket(payment_id)

        total = basket.total_incl_tax
        self.record_processor_response(response, transaction_id=payment_id, basket=basket)

        if amt is not None:
            if amt != total:
                raise GatewayError(
                    'Amt response from spgateway is not as same as basket total. {}!={} basket={}'.format(
                        amt, total, basket.id,
                    )
                )

        if payment_type == 'CREDIT':
            try:
                result = self._credit_close_execute(MerchantOrderNo=payment_id, Amt=amt)
                self.record_processor_response(result, transaction_id=payment_id, basket=basket)
                if result['Status'] == 'SUCCESS':
                    # On success break the loop.
                    logger.info("Successfully executed Spgateway payment [%s] for basket [%d].", payment_id, basket.id)
                else:
                    raise GatewayError(u'{}: {}'.format(result['Status'], result['Message']))
            except GatewayError as e:
                # Spgateway will charge automatically. Pass this exception is fine.
                pass

        currency = basket.currency

        result = response['TradeInfo']['Result']

        total = basket.total_incl_tax
        card_type = result['PaymentType']
        if card_type in ('CREDIT',):
            card_number = '{}{}{}'.format(result.get('Card6No', '*' * 6), '*' * 6, result.get('Card4No', '*' * 4))
        elif card_type in ('WEBATM', 'VACC'):
            card_number = '{}-{}{}'.format(result.get('PayBankCode', '*' * 3), '*' * (14 - 5), result.get('PayerAccount5Code', '*' * 10))
        elif card_type in ('CVS',):
            card_number = '{}'.format(result.get('CodeNo', '*' * 10))
        elif card_type in ('BARCODE',):
            card_number = '{}-{}-{}'.format(result.get('Barcode_1'), result.get('Barcode_2'), result.get('Barcode_3'))
        elif card_type in ('CVSCOM',):
            card_number = '{}'.format('*' * 10)
        transaction_id = result['TradeNo']

        return HandledProcessorResponse(
            transaction_id=transaction_id,
            total=total,
            currency=currency,
            card_number=card_number,
            card_type=card_type,
        )

    def issue_credit(self, order, reference_number, amount, currency):
        basket = order.basket
        result = self._credit_close_execute(
            TradeNo=reference_number,
            Amt=amount,
            CloseType=2,
            IndexType=2,
        )
        self.record_processor_response(result, transaction_id=reference_number, basket=basket)
        if result['Status'] == 'SUCCESS':
            # On success break the loop.
            logger.info("Successfully issue Spgateway payment [%s] for basket [%d].", reference_number, basket.id)
        else:
            raise GatewayError(u'{}: {}'.format(result['Status'], result['Message']))

        return reference_number
