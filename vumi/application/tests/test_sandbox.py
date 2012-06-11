"""Tests for vumi.application.sandbox."""

import os
import sys
import json
import pkg_resources

from twisted.internet.defer import inlineCallbacks
from twisted.internet.error import ProcessTerminated
from twisted.trial.unittest import TestCase

from vumi.message import TransportUserMessage, TransportEvent
from vumi.application.tests.test_base import ApplicationTestCase
from vumi.application.sandbox import (Sandbox, SandboxCommand, SandboxError,
                                      RedisResource)
from vumi.tests.utils import FakeRedis, LogCatcher


class SandboxTestCase(ApplicationTestCase):

    application_class = Sandbox

    def setup_app(self, executable, args, extra_config=None):
        tmp_path = self.mktemp()
        os.mkdir(tmp_path)
        config = {
            'executable': executable,
            'args': args,
            'path': tmp_path,
            'timeout': '10',
            }
        if extra_config is not None:
            config.update(extra_config)
        return self.get_application(config)

    @inlineCallbacks
    def test_bad_command_from_sandbox(self):
        app = yield self.setup_app('/bin/echo', ['-n', '{}'])
        event = TransportEvent(event_type='ack', user_message_id=1,
                               sent_message_id=1, sandbox_id='sandbox1')
        status = yield app.process_event_in_sandbox(event)
        [sandbox_err] = self.flushLoggedErrors(SandboxError)
        self.assertEqual(str(sandbox_err.value).split(' [')[0],
                         "Resource fallback: unknown command 'unknown'"
                         " received from sandbox 'sandbox1'")
        # There are two possible conditions here:
        # 1) The process is killed and terminates with signal 9.
        # 2) The process exits normally before it can be killed and returns
        #    exit status 0.
        if status is None:
            [kill_err] = self.flushLoggedErrors(ProcessTerminated)
            self.assertTrue('process ended by signal' in str(kill_err.value))
        else:
            self.assertEqual(status, 0)

    @inlineCallbacks
    def test_stderr_from_sandbox(self):
        app = yield self.setup_app(sys.executable,
                                   ['-c',
                                    "import sys; sys.stderr.write('err\\n')"])
        event = TransportEvent(event_type='ack', user_message_id=1,
                               sent_message_id=1, sandbox_id='sandbox1')
        status = yield app.process_event_in_sandbox(event)
        self.assertEqual(status, 0)
        [sandbox_err] = self.flushLoggedErrors(SandboxError)
        self.assertEqual(str(sandbox_err.value).split(' [')[0], "err")

    @inlineCallbacks
    def test_resource_setup(self):
        r_server = FakeRedis()
        json_data = SandboxCommand(cmd='db.set', key='foo',
                                   value={'a': 1, 'b': 2}).to_json()
        app = yield self.setup_app('/bin/echo', [json_data], {
            'sandbox': {
                'db': {
                    'cls': 'vumi.application.sandbox.RedisResource',
                    'redis': r_server,
                    'r_prefix': 'test',
                    }
                }
            })
        event = TransportEvent(event_type='ack', user_message_id=1,
                               sent_message_id=1, sandbox_id='sandbox1')
        status = yield app.process_event_in_sandbox(event)
        self.assertEqual(status, 0)
        self.assertEqual(sorted(r_server.keys()),
                         ['test:count:sandbox1',
                          'test:sandboxes:sandbox1:foo'])
        self.assertEqual(r_server.get('test:count:sandbox1'), '1')
        self.assertEqual(r_server.get('test:sandboxes:sandbox1:foo'),
                         json.dumps({'a': 1, 'b': 2}))

    @inlineCallbacks
    def test_outbound_reply_from_sandbox(self):
        msg = TransportUserMessage(to_addr="1", from_addr="2",
                                   transport_name="test",
                                   transport_type="sphex",
                                   sandbox_id='sandbox1')
        json_data = SandboxCommand(cmd='outbound.reply_to',
                                   content='Hooray!',
                                   in_reply_to=msg['message_id']).to_json()
        app = yield self.setup_app('/bin/echo', [json_data], {
            'sandbox': {
                'outbound': {
                    'cls': 'vumi.application.sandbox.OutboundResource',
                    }
                }
            })
        status = yield app.process_message_in_sandbox(msg)
        self.assertEqual(status, 0)
        [reply] = self.get_dispatched_messages()
        self.assertEqual(reply['content'], "Hooray!")
        self.assertEqual(reply['session_event'], None)

    @inlineCallbacks
    def test_js_sandboxer(self):
        msg = TransportUserMessage(to_addr="1", from_addr="2",
                                   transport_name="test",
                                   transport_type="sphex",
                                   sandbox_id='sandbox1')
        sandboxer_js = pkg_resources.resource_filename('vumi.application',
                                                       'sandboxer.js')
        app_js = pkg_resources.resource_filename('vumi.application',
                                                 'app.js')
        app = yield self.setup_app('/usr/local/bin/node',
                                   [sandboxer_js, app_js], {
            'sandbox': {
                'log': {
                    'cls': 'vumi.application.sandbox.LoggingResource',
                    }
                }
            })

        with LogCatcher() as lc:
            status = yield app.process_message_in_sandbox(msg)
            failures = [log['failure'].value for log in lc.errors]
            msgs = [log['message'][0] for log in lc.logs if log['message']]
        self.assertEqual(failures, [])
        self.assertEqual(status, 0)
        self.assertEqual(msgs, [
            'Loading sandboxed code ...',
            'Starting sandbox ...',
            'From init!',
            'Sandbox running ...',
            'From command: initialize',
            'From command: inbound-message',
            'Log successful: true',
            'Done.',
            ])

    # TODO: test process killed if it writes too much.

    # TODO: def consume_user_message(self, msg):
    # TODO: def close_session(self, msg):
    # TODO: def consume_ack(self, event):
    # TODO: def consume_delivery_report(self, event):


class DummyAppWorker(object):

    class DummyApi(object):
        def __init__(self, sandbox_id):
            self.sandbox_id = sandbox_id

    class DummyProtocol(object):
        def __init__(self, api):
            self.api = api

    sandbox_api_cls = DummyApi
    sandbox_protocol_cls = DummyProtocol

    def create_sandbox_api(self, sandbox_id):
        return self.sandbox_api_cls(sandbox_id)

    def create_sandbox_protocol(self, api):
        return self.sandbox_protocol_cls(api)


class ResourceTestCaseBase(TestCase):

    app_worker_cls = DummyAppWorker
    resource_cls = None
    resource_name = 'test_resource'
    sandbox_id = 'test_id'

    def setUp(self):
        self.app_worker = self.app_worker_cls()
        self.resource = None

    def tearDown(self):
        if self.resource is not None:
            self.resource.teardown()

    def create_resource(self, config):
        resource = self.resource_cls(self.resource_name,
                                     self.app_worker,
                                     config)
        resource.setup()
        self.resource = resource

    def dispatch_command(self, cmd, **kwargs):
        if self.resource is None:
            raise ValueError("Create a resource before"
                             " calling dispatch_command")
        msg = SandboxCommand(cmd=cmd, **kwargs)
        api = self.app_worker.create_sandbox_api(self.sandbox_id)
        sandbox = self.app_worker.create_sandbox_protocol(api)
        return self.resource.dispatch_request(api, sandbox, msg)


class TestRedisResource(ResourceTestCaseBase):

    resource_cls = RedisResource

    def setUp(self):
        super(TestRedisResource, self).setUp()
        self.r_server = FakeRedis()
        self.create_resource({
            'r_prefix': 'test',
            'redis': self.r_server,
            })

    def tearDown(self):
        super(TestRedisResource, self).tearDown()
        self.r_server.teardown()

    def test_handle_set(self):
        reply = self.dispatch_command('set', key='foo', value='bar')
        self.assertEqual(reply['success'], True)
        self.assertEqual(self.r_server.get('test:sandboxes:test_id:foo'),
                         json.dumps('bar'))
        self.assertEqual(self.r_server.get('test:count:test_id'), '1')

    def test_handle_get(self):
        self.r_server.set('test:sandboxes:test_id:foo', json.dumps('bar'))
        reply = self.dispatch_command('get', key='foo')
        self.assertEqual(reply['success'], True)
        self.assertEqual(reply['value'], 'bar')

    def test_handle_delete(self):
        self.r_server.set('test:sandboxes:test_id:foo', json.dumps('bar'))
        self.r_server.set('test:count:test_id', '1')
        reply = self.dispatch_command('delete', key='foo')
        self.assertEqual(reply['success'], True)
        self.assertEqual(reply['existed'], True)
        self.assertEqual(self.r_server.get('test:sandboxes:test_id:foo'), None)
        self.assertEqual(self.r_server.get('test:count:test_id'), '0')


class TestOutboundResource(TestCase):
    pass

    # TODO: complete


class TestLoggingResource(TestCase):
    pass

    # TODO: complete
