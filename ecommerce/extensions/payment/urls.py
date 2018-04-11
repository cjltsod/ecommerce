from django.conf.urls import include, url

from ecommerce.extensions.payment.views import PaymentFailedView, SDNFailure, cybersource, paypal, spgateway

CYBERSOURCE_URLS = [
    url(r'^redirect/$', cybersource.CybersourceInterstitialView.as_view(), name='redirect'),
    url(r'^submit/$', cybersource.CybersourceSubmitView.as_view(), name='submit'),
]

PAYPAL_URLS = [
    url(r'^execute/$', paypal.PaypalPaymentExecutionView.as_view(), name='execute'),
    url(r'^profiles/$', paypal.PaypalProfileAdminView.as_view(), name='profiles'),
]

SDN_URLS = [
    url(r'^failure/$', SDNFailure.as_view(), name='failure'),
]

SPGATEWAY_URLS = [
    url(r'^return/$', spgateway.SpgatewayReturnView.as_view(), name='return'),
    url(r'^notify/$', spgateway.SpgatewayNotifyView.as_view(), name='notify'),
    url(r'^customer/$', spgateway.SpgatewayCustomerView.as_view(), name='customer'),
]

SPGATEWAY_LOCAL_TEST_URLS = [
    url(r'^return/$', spgateway.SpgatewayNotifyReturnView.as_view(), name='return'),
    url(r'^notify/$', spgateway.SpgatewayNotifyReturnView.as_view(), name='notify'),
    url(r'^customer/$', spgateway.SpgatewayCustomerView.as_view(), name='customer'),
]

urlpatterns = [
    url(r'^cybersource/', include(CYBERSOURCE_URLS, namespace='cybersource')),
    url(r'^error/$', PaymentFailedView.as_view(), name='payment_error'),
    url(r'^paypal/', include(PAYPAL_URLS, namespace='paypal')),
    url(r'^sdn/', include(SDN_URLS, namespace='sdn')),
    url(r'^spgateway/', include(SPGATEWAY_URLS, namespace='spgateway')),
]
