import json
from urllib import urlencode
from datetime import datetime

from twisted.internet.defer import inlineCallbacks

from vumi.utils import http_request_full
from vumi.message import TransportUserMessage
from vumi.transports.tests.utils import TransportTestCase
from vumi.transports.imimobile import ImiMobileUssdTransport


class TestImiMobileUssdTransportTestCase(TransportTestCase):

    transport_class = ImiMobileUssdTransport
    timeout = 1

    _from_addr = '9221234567'
    _to_addr = '56263'
    _request_defaults = {
        'msisdn': _from_addr,
        'msg': 'Spam Spam Spam Spam Spammity Spam',
        'tid': '1',
        'dcs': 'no-idea-what-this-is',
        'code': 'VUMI',
    }

    @inlineCallbacks
    def setUp(self):
        yield super(TestImiMobileUssdTransportTestCase, self).setUp()
        self.config = {
            'web_port': 0,
            'web_path': '/api/v1/imimobile/ussd/',
            'user_terminated_session_message': "^Farewell",
            'user_terminated_session_response': "You have ended the session",
            'suffix_to_addrs': {
                'some-suffix': self._to_addr,
                'some-other-suffix': '56264',
             }
        }
        self.transport = yield self.get_transport(self.config)
        self.session_manager = self.transport.session_manager
        self.transport_url = self.transport.get_transport_url(
            self.config['web_path'])
        yield self.session_manager.redis._purge_all()  # just in case

    def mk_full_request(self, suffix, **params):
        return http_request_full('%s?%s' % (self.transport_url + suffix,
            urlencode(params)), data='/api/v1/imimobile/ussd/', method='GET')

    def mk_request(self, suffix, **params):
        defaults = {}
        defaults.update(self._request_defaults)
        defaults.update(params)
        return self.mk_full_request(suffix, **defaults)

    def mk_reply(self, request_msg, reply_content, continue_session=True):
        request_msg = TransportUserMessage(**request_msg.payload)
        return request_msg.reply(reply_content, continue_session)

    @inlineCallbacks
    def mk_session(self, from_addr=_from_addr, to_addr=_to_addr):
        # first pre-populate the redis datastore to simulate session resume
        # note: imimobile do not provide a session id, so instead we use the
        # msisdn as the session id
        yield self.session_manager.create_session(
            from_addr, to_addr=to_addr, from_addr=from_addr)

    def assert_message(self, msg, expected_field_values):
        for field, expected_value in expected_field_values.iteritems():
            self.assertEqual(msg[field], expected_value)

    def assert_inbound_message(self, msg, **field_values):
        expected_field_values = {
            'content': self._request_defaults['msg'],
            'to_addr': '56263',
            'from_addr': self._request_defaults['msisdn'],
            'session_event': TransportUserMessage.SESSION_NEW,
            'transport_metadata': {
                'imimobile_ussd': {
                    'tid': self._request_defaults['tid'],
                    'dcs': self._request_defaults['dcs'],
                    'code': self._request_defaults['code'],
                },
            }
        }
        expected_field_values.update(field_values)

        for field, expected_value in expected_field_values.iteritems():
            self.assertEqual(msg[field], expected_value)

    def assert_ack(self, ack, reply):
        self.assertEqual(ack.payload['event_type'], 'ack')
        self.assertEqual(ack.payload['user_message_id'], reply['message_id'])
        self.assertEqual(ack.payload['sent_message_id'], reply['message_id'])

    def assert_nack(self, nack, reply, reason):
        self.assertEqual(nack.payload['event_type'], 'nack')
        self.assertEqual(nack.payload['user_message_id'], reply['message_id'])
        self.assertEqual(nack.payload['nack_reason'], reason)

    @inlineCallbacks
    def test_inbound_begin(self):
        # Second connect is the actual start of the session
        user_content = "Who are you?"
        d = self.mk_request('some-suffix', msg=user_content)
        [msg] = yield self.wait_for_dispatched_messages(1)
        self.assert_inbound_message(msg,
            session_event=TransportUserMessage.SESSION_NEW,
            content=user_content)

        reply_content = "We are the Knights Who Say ... Ni!"
        reply = self.mk_reply(msg, reply_content)
        self.dispatch(reply)
        response = yield d
        self.assertEqual(response.delivered_body, reply_content)
        self.assertEqual(
            response.headers.getRawHeaders('X-USSD-SESSION'), ['1'])

        [ack] = yield self.wait_for_dispatched_events(1)
        self.assert_ack(ack, reply)

    @inlineCallbacks
    def test_inbound_resume_and_reply_with_end(self):
        from_addr = '9221234567'
        yield self.mk_session(from_addr)

        user_content = "Well, what is it you want?"
        d = self.mk_request('some-suffix', msg=user_content)
        [msg] = yield self.wait_for_dispatched_messages(1)
        self.assert_inbound_message(msg,
            session_event=TransportUserMessage.SESSION_RESUME,
            content=user_content)

        reply_content = "We want ... a shrubbery!"
        reply = self.mk_reply(msg, reply_content, continue_session=False)
        self.dispatch(reply)
        response = yield d
        self.assertEqual(response.delivered_body, reply_content)
        self.assertEqual(
            response.headers.getRawHeaders('X-USSD-SESSION'), ['0'])

        # Assert that the session was removed from the session manager
        session = yield self.session_manager.load_session(from_addr)
        self.assertEqual(session, {})

        [ack] = yield self.wait_for_dispatched_events(1)
        self.assert_ack(ack, reply)

    @inlineCallbacks
    def test_inbound_resume_and_reply_with_resume(self):
        yield self.mk_session()

        user_content = "Well, what is it you want?"
        d = self.mk_request('some-suffix', msg=user_content)
        [msg] = yield self.wait_for_dispatched_messages(1)
        self.assert_inbound_message(msg,
            session_event=TransportUserMessage.SESSION_RESUME,
            content=user_content)

        reply_content = "We want ... a shrubbery!"
        reply = self.mk_reply(msg, reply_content, continue_session=True)
        self.dispatch(reply)
        response = yield d
        self.assertEqual(response.delivered_body, reply_content)
        self.assertEqual(
            response.headers.getRawHeaders('X-USSD-SESSION'), ['1'])

        [ack] = yield self.wait_for_dispatched_events(1)
        self.assert_ack(ack, reply)

    @inlineCallbacks
    def test_inbound_close_and_reply(self):
        from_addr = '9221234567'
        self.mk_session(from_addr=from_addr)

        user_content = "Farewell, sweet Concorde!"
        d = self.mk_request('some-suffix', msg=user_content)
        [msg] = yield self.wait_for_dispatched_messages(1)
        self.assert_inbound_message(msg,
            session_event=TransportUserMessage.SESSION_CLOSE,
            content=user_content)

        # Assert that the session was removed from the session manager
        session = yield self.session_manager.load_session(from_addr)
        self.assertEqual(session, {})

        response = yield d
        self.assertEqual(response.delivered_body, "You have ended the session")
        self.assertEqual(
            response.headers.getRawHeaders('X-USSD-SESSION'), ['0'])

    @inlineCallbacks
    def test_request_with_unknown_suffix(self):
        response = yield self.mk_request('unk-suffix')

        self.assertEqual(
            response.delivered_body,
            json.dumps({'unknown_suffix': 'unk-suffix'}))
        self.assertEqual(response.code, 400)

    @inlineCallbacks
    def test_request_with_missing_parameters(self):
        response = yield self.mk_full_request(
            'some-suffix', msg='', code='', dcs='')

        self.assertEqual(
            response.delivered_body,
            json.dumps({'missing_parameter': ['msisdn', 'tid']}))
        self.assertEqual(response.code, 400)

    @inlineCallbacks
    def test_request_with_unexpected_parameters(self):
        response = yield self.mk_request(
            'some-suffix', unexpected_p1='', unexpected_p2='')

        self.assertEqual(
            response.delivered_body,
            json.dumps({
                'unexpected_parameter': ['unexpected_p1', 'unexpected_p2']
            }))
        self.assertEqual(response.code, 400)

    @inlineCallbacks
    def test_nack_insufficient_message_fields(self):
        reply = self.mkmsg_out(message_id='23', in_reply_to=None, content=None)
        self.dispatch(reply)
        [nack] = yield self.wait_for_dispatched_events(1)
        self.assert_nack(
            nack, reply, self.transport.INSUFFICIENT_MSG_FIELDS_ERROR)

    @inlineCallbacks
    def test_nack_http_http_response_failure(self):
        self.patch(self.transport, 'finish_request', lambda *a, **kw: None)
        reply = self.mkmsg_out(
            message_id='23',
            in_reply_to='some-number',
            content='There are some who call me ... Tim!')
        self.dispatch(reply)
        [nack] = yield self.wait_for_dispatched_events(1)
        self.assert_nack(
            nack, reply, self.transport.RESPONSE_FAILURE_ERROR)

    def test_ist_to_utc(self):
        self.assertEqual(
            ImiMobileUssdTransport.ist_to_utc("1/26/2013 03:30:00 pm"),
            datetime(2013, 1, 26, 10, 0, 0))

        self.assertEqual(
            ImiMobileUssdTransport.ist_to_utc("01/29/2013 04:53:59 am"),
            datetime(2013, 1, 28, 23, 23, 59))

        self.assertEqual(
            ImiMobileUssdTransport.ist_to_utc("01/31/2013 07:20:00 pm"),
            datetime(2013, 1, 31, 13, 50, 0))

        self.assertEqual(
            ImiMobileUssdTransport.ist_to_utc("3/8/2013 8:5:5 am"),
            datetime(2013, 3, 8, 2, 35, 5))
