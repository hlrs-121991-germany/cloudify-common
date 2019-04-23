########
# Copyright (c) 2013 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
#    * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    * See the License for the specific language governing permissions and
#    * limitations under the License.

from collections import deque
import copy
import json
import logging
import os
import Queue
import random
import ssl
import sys
import threading
import time
import uuid

import pika
import pika.exceptions

from cloudify import exceptions
from cloudify import broker_config
from cloudify.constants import EVENTS_EXCHANGE_NAME, LOGS_EXCHANGE_NAME


logger = logging.getLogger(__name__)

if sys.version_info >= (2, 7):
    # requires 2.7+
    def wait_for_event(evt, poll_interval=0.5):
        """Wait for a threading.Event by polling, to allow handling of signals.
        (ie. doesnt block ^C)
        """
        while True:
            if evt.wait(poll_interval):
                return
else:
    def wait_for_event(evt, poll_interval=None):
        """Wait for a threading.Event. Stub for compatibility."""
        # in python 2.6, Event.wait always returns None, so we can either:
        #  - .wait() without a timeout and block ^C which is inconvenient
        #  - .wait() with timeout and then check .is_set(),
        #     which is not threadsafe
        # We choose the inconvenient but safe method.
        evt.wait()


class AMQPParams(object):
    def __init__(self,
                 amqp_host=None,
                 amqp_user=None,
                 amqp_pass=None,
                 amqp_port=None,
                 amqp_vhost=None,
                 ssl_enabled=None,
                 ssl_cert_path=None,
                 socket_timeout=3,
                 heartbeat_interval=None):
        super(AMQPParams, self).__init__()
        username = amqp_user or broker_config.broker_username
        password = amqp_pass or broker_config.broker_password
        heartbeat = heartbeat_interval or broker_config.broker_heartbeat
        credentials = pika.credentials.PlainCredentials(
            username=username,
            password=password,
        )

        broker_ssl_options = {}
        if ssl_enabled:
            broker_ssl_options = {
                'ca_certs': ssl_cert_path,
                'cert_reqs': ssl.CERT_REQUIRED,
            }
        if not broker_ssl_options:
            broker_ssl_options = broker_config.broker_ssl_options

        self.raw_host = amqp_host or broker_config.broker_hostname
        self._amqp_params = {
            'port': amqp_port or broker_config.broker_port,
            'virtual_host': amqp_vhost or broker_config.broker_vhost,
            'credentials': credentials,
            'ssl': ssl_enabled or broker_config.broker_ssl_enabled,
            'ssl_options': broker_ssl_options,
            'heartbeat': heartbeat,
            'socket_timeout': socket_timeout
        }

    def as_pika_params(self):
        return pika.ConnectionParameters(**self._amqp_params)


class ConnectionTimeoutError(Exception):
    """Timeout trying to connect"""


def _get_daemon_factory():
    """
    We need the factory to dynamically load daemon config, to support
    HA failovers
    """
    # Dealing with circular dependency
    try:
        from cloudify_agent.api.factory import DaemonFactory
    except ImportError:
        # Might not exist in e.g. the REST service
        DaemonFactory = None
    return DaemonFactory


class AMQPConnection(object):
    MAX_BACKOFF = 30

    def __init__(self, handlers, name=None, amqp_params=None,
                 connect_timeout=10):
        self._handlers = handlers
        self.name = name
        self._connection_params = self._get_connection_params()
        self._reconnect_backoff = 1
        self._closed = False
        self._amqp_params = amqp_params or AMQPParams()
        self._pika_connection = None
        self._consumer_thread = None
        self.connect_wait = threading.Event()
        self._connect_timeout = connect_timeout
        self._error = None
        self._daemon_factory = _get_daemon_factory()

        # use this queue to schedule methods to be called on the pika channel
        # from the connection thread - for sending data to rabbitmq, eg.
        # publishing messages or sending ACKs, which needs to be done from
        # the connection thread
        self._connection_tasks_queue = Queue.Queue()

    def _get_connection_params(self):
        params = self._amqp_params.as_pika_params()
        hosts = copy.copy(self._amqp_params.raw_host)
        if isinstance(hosts, basestring):
            hosts = [hosts]
        else:
            random.shuffle(hosts)
        while True:
            for host in hosts:
                params.host = host
                logger.debug('Current connection params: {0}'.format(params))
                yield params

    def _get_reconnect_backoff(self):
        backoff = self._reconnect_backoff
        self._reconnect_backoff = min(backoff * 2, self.MAX_BACKOFF)
        return backoff

    def _reset_reconnect_backoff(self):
        self._reconnect_backoff = 1

    def connect(self):
        self._error = None
        deadline = None
        self._pika_connection = None
        if self._connect_timeout is not None:
            deadline = time.time() + self._connect_timeout

        try:
            while self._pika_connection is None:
                params = next(self._connection_params)
                self._pika_connection = self._get_pika_connection(
                    params, deadline)
        # unfortunately DaemonNotFoundError is a BaseException subclass :(
        except BaseException as e:
            self._error = e
            self.connect_wait.set()
            raise e

        out_channel = self._pika_connection.channel()
        out_channel.confirm_delivery()
        for handler in self._handlers:
            handler.register(self)
            logger.info('Registered handler for {0} [{1}]'
                        .format(handler.__class__.__name__,
                                handler.routing_key))
        self.connect_wait.set()
        return out_channel

    def _get_pika_connection(self, params, deadline=None):
        try:
            connection = pika.BlockingConnection(params)
        except pika.exceptions.AMQPConnectionError as e:
            time.sleep(self._get_reconnect_backoff())
            if deadline and time.time() > deadline:
                raise e
        else:
            self._reset_reconnect_backoff()
            self._closed = False
            return connection

    def consume(self):
        out_channel = self.connect()
        while not self._closed:
            try:
                self._pika_connection.process_data_events(0.2)
                self._process_publish(out_channel)
            except pika.exceptions.ChannelClosed as e:
                # happens when we attempt to use an exchange/queue that is not
                # declared - nothing we can do to help it, just exit
                logger.error('Channel closed: {0}'.format(e))
                break
            except pika.exceptions.ConnectionClosed:
                self.connect_wait.clear()
                out_channel = self.connect()
                continue
        self._process_publish(out_channel)
        self._pika_connection.close()

    def consume_in_thread(self):
        """Spawn a thread to run consume"""
        if self._consumer_thread:
            return
        self._consumer_thread = threading.Thread(target=self.consume)
        self._consumer_thread.daemon = True
        self._consumer_thread.start()
        wait_for_event(self.connect_wait)

        if self._error is not None:
            raise self._error
        return self._consumer_thread

    def __enter__(self):
        self.consume_in_thread()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _process_publish(self, channel):
        while True:
            try:
                envelope = self._connection_tasks_queue.get_nowait()
            except Queue.Empty:
                return

            target_channel = envelope['channel'] or channel
            method = envelope['method']
            # we use a separate queue to send any possible exceptions back
            # to the calling thread - see the publish method
            message = envelope['message']
            err_queue = envelope.get('err_queue')

            try:
                getattr(target_channel, method)(**message)
            except pika.exceptions.ConnectionClosed:
                if self._closed:
                    return
                # if we couldn't send the message because the connection
                # was down, requeue it to be sent again later
                self._connection_tasks_queue.put(envelope)
                raise
            except Exception as e:
                if err_queue:
                    err_queue.put(e)
                raise
            else:
                if err_queue:
                    err_queue.put(None)

    def close(self, wait=True):
        self._closed = True
        if self._consumer_thread and wait:
            self._consumer_thread.join()
            self._consumer_thread = None

    def add_handler(self, handler):
        self._handlers.append(handler)
        if self._pika_connection:
            handler.register(self)

    def channel(self):
        if self._closed or not self._pika_connection:
            raise RuntimeError(
                'Attempted to open a channel on a closed connection')
        return self._pika_connection.channel()

    def channel_method(self, method, channel=None, wait=True,
                       timeout=None, **kwargs):
        """Schedule a channel method to be called from the connection thread.

        Use this to schedule a channel method such as .publish or .basic_ack
        to be called from the connection thread.
        """
        if wait and self._consumer_thread \
                and self._consumer_thread is threading.current_thread():
            # when sending from the connection thread, we can't wait because
            # then we wouldn't allow the actual send loop (._process_publish)
            # to run, because we'd block on the err_queue here
            raise RuntimeError(
                'Cannot wait when sending from the connection thread')

        # the message is going to be sent from another thread (the .consume
        # thread). If an error happens there, we must have a way to get it
        # back out, so we pass a Queue together with the message, that will
        # contain either an exception instance, or None
        err_queue = Queue.Queue() if wait else None
        envelope = {
            'method': method,
            'message': kwargs,
            'err_queue': err_queue,
            'channel': channel
        }
        self._connection_tasks_queue.put(envelope)
        if err_queue:
            err = err_queue.get(timeout=timeout)
            if isinstance(err, Exception):
                raise err

    def publish(self, message, wait=True, timeout=None):
        """Schedule a message to be sent.

        :param message: Kwargs for the pika basic_publish call. Should at
                        least contain the "body" and "exchange" keys, and
                        it might contain other keys such as "routing_key"
                        or "properties"
        :param wait: Whether to wait for the message to actually be sent.
                     If true, an exception will be raised if the message
                     cannot be sent.
        """
        self.channel_method('publish', wait=wait, timeout=timeout, **message)

    def ack(self, channel, delivery_tag, wait=True, timeout=None):
        self.channel_method('basic_ack', wait=wait, timeout=timeout,
                            channel=channel, delivery_tag=delivery_tag)


class TaskConsumer(object):
    routing_key = ''
    late_ack = False

    def __init__(self, queue, threadpool_size=5, exchange_type='direct'):
        self.threadpool_size = threadpool_size
        self.exchange = queue
        self.queue = '{0}_{1}'.format(queue, self.routing_key)
        self._sem = threading.Semaphore(threadpool_size)
        self._connection = None
        self.in_channel = None
        self.exchange_type = exchange_type
        self._tasks_buffer = deque()

    def register(self, connection):
        self._connection = connection
        self.in_channel = connection.channel()
        self._register_queue(self.in_channel)

    def _register_queue(self, channel):
        channel.basic_qos(prefetch_count=self.threadpool_size)
        channel.confirm_delivery()
        channel.exchange_declare(exchange=self.exchange,
                                 auto_delete=False,
                                 durable=True,
                                 exchange_type=self.exchange_type)
        channel.queue_declare(queue=self.queue,
                              durable=True,
                              auto_delete=False)
        channel.queue_bind(queue=self.queue,
                           exchange=self.exchange,
                           routing_key=self.routing_key)
        channel.basic_consume(self.process, self.queue)

    def process(self, channel, method, properties, body):
        try:
            full_task = json.loads(body)
        except ValueError:
            logger.error('Error parsing task: {0}'.format(body))
            return

        task_args = (channel, properties, full_task, method.delivery_tag)
        if self._sem.acquire(blocking=False):
            self._run_task(task_args)
        else:
            self._tasks_buffer.append(task_args)

    def _process_message(self, channel, properties, full_task, delivery_tag):
        if not self.late_ack:
            self._connection.ack(channel, delivery_tag)
        try:
            result = self.handle_task(full_task)
        except Exception as e:
            result = {'ok': False, 'error': repr(e)}
            logger.exception(
                'ERROR - failed message processing: '
                '{0!r}\nbody: {1}'.format(e, full_task)
            )
        if self.late_ack:
            self._connection.ack(channel, delivery_tag)
        if properties.reply_to:
            self._connection.publish({
                'exchange': self.exchange,
                'routing_key': properties.reply_to,
                'properties': pika.BasicProperties(
                    correlation_id=properties.correlation_id),
                'body': json.dumps(result)
            })
        if not self._maybe_run_next_task():
            self._sem.release()

    def _run_task(self, task_args):
        new_thread = threading.Thread(
            target=self._process_message,
            args=task_args
        )
        new_thread.daemon = True
        new_thread.start()

    def _maybe_run_next_task(self):
        try:
            task_args = self._tasks_buffer.popleft()
        except IndexError:
            return False
        else:
            self._run_task(task_args)
            return True

    def handle_task(self, full_task):
        raise NotImplementedError()

    def delete_queue(self, queue):
        self._connection.channel_method(
            'queue_delete', queue=queue, channel=self.in_channel,
            if_empty=True)

    def delete_exchange(self, exchange):
        self._connection.channel_method(
            'exchange_delete', exchange=exchange, channel=self.in_channel)


class SendHandler(object):
    exchange_settings = {
        'auto_delete': False,
        'durable': True,
    }
    wait_for_publish = True

    def __init__(self, exchange, exchange_type='direct', routing_key=''):
        self.exchange = exchange
        self.exchange_type = exchange_type
        self.routing_key = routing_key
        self.logger = logging.getLogger('dispatch.{0}'.format(self.exchange))
        self._connection = None

    def register(self, connection):
        self._connection = connection
        out_channel = connection.channel()
        out_channel.exchange_declare(exchange=self.exchange,
                                     exchange_type=self.exchange_type,
                                     **self.exchange_settings)

    def _log_message(self, message):
        level = message.get('level', 'info')
        log_func = getattr(self.logger, level, self.logger.info)
        exec_id = message.get('context', {}).get('execution_id')
        text = message['message']['text']
        msg = '[{0}] {1}'.format(exec_id, text) if exec_id else text
        log_func(msg)

    def publish(self, message, **kwargs):
        if 'message' in message:
            # message is textual, let's log it
            self._log_message(message)
        self._connection.publish({
            'exchange': self.exchange,
            'body': json.dumps(message),
            'routing_key': self.routing_key
        }, wait=self.wait_for_publish)


class ScheduledExecutionHandler(SendHandler):

    def __init__(self, exchange, exchange_type, routing_key,
                 target_exchange, target_routing_key, ttl):

        super(ScheduledExecutionHandler, self).__init__(exchange,
                                                        exchange_type,
                                                        routing_key)
        # The ultimate exchange and queue the message will be sent to (from
        #  the Dead Letter queue)
        self.target_exchange = target_exchange
        self.target_routing_key = target_routing_key
        self.ttl = ttl

    def register(self, connection):
        self._connection = connection

        out_channel = connection.channel()
        out_channel.exchange_declare(exchange=self.exchange,
                                     exchange_type=self.exchange_type,
                                     **self.exchange_settings)
        # Declare a new temporary queue for the Dead Letter Exchange, and
        # set the routing key of the MGMTWORKER queue
        out_channel.queue_declare(queue=self.routing_key,
                                  arguments={
                                      'x-message-ttl': self.ttl,
                                      'x-dead-letter-exchange': (
                                          self.target_exchange
                                      ),
                                      'x-dead-letter-routing-key': (
                                          self.target_routing_key
                                      ),
                                  },
                                  durable=True)
        out_channel.queue_bind(exchange=self.exchange, queue=self.routing_key)


class NoWaitSendHandler(SendHandler):
    """
    A send handler that doesn't wait for the message to be sent.
    This is useful for buffering cases like sending multiple logs at once.
    """
    wait_for_publish = False


class _RequestResponseHandlerBase(TaskConsumer):
    def __init__(self, exchange, queue=None):
        super(_RequestResponseHandlerBase, self).__init__(exchange)
        self.queue = queue or '{0}_response_{1}'.format(
            self.exchange, uuid.uuid4().hex)

    def _register_queue(self, channel):
        self.in_channel.exchange_declare(exchange=self.exchange,
                                         auto_delete=False,
                                         durable=True,
                                         exchange_type=self.exchange_type)
        self.in_channel.queue_declare(queue=self.queue, exclusive=True,
                                      durable=True)
        self.in_channel.queue_bind(queue=self.queue, exchange=self.exchange)
        channel.basic_consume(self.process, self.queue)

    def publish(self, message, correlation_id, routing_key='',
                expiration=None):
        if expiration is not None:
            # rabbitmq wants it to be a string
            expiration = '{0}'.format(expiration)
        self._connection.publish({
            'exchange': self.exchange,
            'body': json.dumps(message),
            'properties': pika.BasicProperties(
                reply_to=self.queue,
                correlation_id=correlation_id,
                expiration=expiration),
            'routing_key': routing_key
        })

    def process(self, channel, method, properties, body):
        raise NotImplementedError()


class BlockingRequestResponseHandler(_RequestResponseHandlerBase):
    def __init__(self, *args, **kwargs):
        super(BlockingRequestResponseHandler, self).__init__(*args, **kwargs)
        self._response_queues = {}

    def publish(self, message, *args, **kwargs):
        timeout = kwargs.pop('timeout', None)
        correlation_id = kwargs.pop('correlation_id', None)
        if correlation_id is None:
            correlation_id = uuid.uuid4().hex
        self._response_queues[correlation_id] = Queue.Queue()
        super(BlockingRequestResponseHandler, self).publish(
            message, correlation_id, *args, **kwargs)
        try:
            resp = self._response_queues[correlation_id].get(timeout=timeout)
            return resp
        except Queue.Empty:
            raise RuntimeError('No response received for task {0}'
                               .format(correlation_id))
        finally:
            del self._response_queues[correlation_id]

    def process(self, channel, method, properties, body):
        channel.basic_ack(method.delivery_tag)
        try:
            response = json.loads(body)
        except ValueError:
            logger.error('Error parsing response: {0}'.format(body))
            return
        if properties.correlation_id in self._response_queues:
            self._response_queues[properties.correlation_id].put(response)


class CallbackRequestResponseHandler(_RequestResponseHandlerBase):
    def __init__(self, *args, **kwargs):
        super(CallbackRequestResponseHandler, self).__init__(*args, **kwargs)
        self._callbacks = {}

    def publish(self, message, *args, **kwargs):
        callback = kwargs.pop('callback', None)
        correlation_id = kwargs.pop('correlation_id', None)
        if correlation_id is None:
            correlation_id = uuid.uuid4().hex
        if callback:
            self.wait_for_response(correlation_id, callback)
        super(CallbackRequestResponseHandler, self).publish(
            message, correlation_id, *args, **kwargs)

    def wait_for_response(self, correlation_id, callback):
        self._callbacks[correlation_id] = callback

    def process(self, channel, method, properties, body):
        channel.basic_ack(method.delivery_tag)
        try:
            response = json.loads(body)
        except ValueError:
            logger.error('Error parsing response: {0}'.format(body))
            return
        if properties.correlation_id in self._callbacks:
            self._callbacks[properties.correlation_id](response)


def get_client(amqp_host=None,
               amqp_user=None,
               amqp_pass=None,
               amqp_port=None,
               amqp_vhost=None,
               ssl_enabled=None,
               ssl_cert_path=None,
               name=None,
               connect_timeout=10,
               cls=AMQPConnection):
    """
    Create a client without any handlers in it. Use the `add_handler` method
    to add handlers to this client
    :return: CloudifyConnectionAMQPConnection
    """

    amqp_params = AMQPParams(
        amqp_host,
        amqp_user,
        amqp_pass,
        amqp_port,
        amqp_vhost,
        ssl_enabled,
        ssl_cert_path
    )

    return cls(handlers=[], amqp_params=amqp_params, name=name,
               connect_timeout=connect_timeout)


class CloudifyEventsPublisher(object):
    SOCKET_TIMEOUT = 5
    CONNECTION_ATTEMPTS = 3
    channel_settings = {
        'auto_delete': False,
        'durable': True,
    }

    def __init__(self, amqp_params):
        self.handlers = {
            'log': NoWaitSendHandler(LOGS_EXCHANGE_NAME,
                                     exchange_type='fanout'),
            'event': SendHandler(EVENTS_EXCHANGE_NAME,
                                 exchange_type='topic',
                                 routing_key='events'),
            'hook': SendHandler(EVENTS_EXCHANGE_NAME,
                                exchange_type='topic',
                                routing_key='events.hooks'),
        }

        self._connection = AMQPConnection(
            handlers=self.handlers.values(),
            amqp_params=amqp_params,
            name=os.environ.get('AGENT_NAME')
        )
        self._is_closed = False

    def connect(self):
        self._connection.consume_in_thread()

    def publish_message(self, message, message_type):
        if self._is_closed:
            raise exceptions.ClosedAMQPClientException(
                'Publish failed, AMQP client already closed')

        handler = self.handlers.get(message_type)

        if handler:
            handler.publish(message)
        else:
            logger.error('Unknown message type : {0} for message : {1}'.
                         format(message_type, message))

    def close(self):
        if self._is_closed:
            return
        self._is_closed = True
        thread = threading.current_thread()
        if self._connection:
            logger.debug('Closing amqp client of thread {0}'.format(thread))
            try:
                self._connection.close()
            except Exception as e:
                logger.debug('Failed to close amqp client of thread {0}, '
                             'reported error: {1}'.format(thread, repr(e)))


def create_events_publisher(amqp_host=None,
                            amqp_user=None,
                            amqp_pass=None,
                            amqp_port=None,
                            amqp_vhost=None,
                            ssl_enabled=None,
                            ssl_cert_path=None):
    thread = threading.current_thread()

    amqp_params = AMQPParams(
        amqp_host,
        amqp_user,
        amqp_pass,
        amqp_port,
        amqp_vhost,
        ssl_enabled,
        ssl_cert_path
    )

    try:
        client = CloudifyEventsPublisher(amqp_params)
        client.connect()
        logger.debug('AMQP client created for thread {0}'.format(thread))
    except Exception as e:
        logger.warning(
            'Failed to create AMQP client for thread: {0} ({1}: {2})'
            .format(thread, type(e).__name__, e))
        raise
    return client
