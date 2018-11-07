"""
NOTE:
 - Task: used to refer to an asyncio task
 - Job: used to refer a specification of some work that has to be done
"""

from guillotina import app_settings
from guillotina_amqp import amqp
from guillotina_amqp.job import Job
from guillotina_amqp.state import get_state_manager, TaskState

import asyncio
import json
import logging
import time


logger = logging.getLogger('guillotina_amqp')


class Worker:
    """Workers hold an asyncio loop in which will run several tasks. It
    reads from RabbitMQ for new job descriptions and will run them in
    asyncio tasks.

    The worker is aware of a state manager in which he posts job
    results.

    """
    sleep_interval = 0.05
    last_activity = time.time()
    total_run = 0

    def __init__(self, request=None, loop=None, max_size=5):
        self.request = request
        self.loop = loop
        self._running = []
        self._done = []
        self._max_size = max_size
        self._closing = False
        self._state_manager = None

    @property
    def state_manager(self):
        if self._state_manager is None:
            self._state_manager = get_state_manager()
        return self._state_manager

    @property
    def num_running(self):
        """Returns the number of currently running jobs"""
        return len(self._running)

    async def handle_queued_job(self, channel, body, envelope, properties):
        """Callback triggered when there is a new job in the job channel (e.g:
        a new task in a rabbitmq queue)

        """
        # Deserialize job description
        if not isinstance(body, str):
            body = body.decode('utf-8')
        data = json.loads(body)

        # Set status to scheduled
        task_id = data['task_id']
        dotted_name = data['func']
        await self.state_manager.update(task_id, {
            'status': 'scheduled'
        })
        logger.info(f'Received task: {task_id}: {dotted_name}')

        # Block if we reached maximum number of running tasks
        while len(self._running) >= self._max_size:
            await asyncio.sleep(self.sleep_interval)
            self.last_activity = time.time()

        # Create job object
        self.last_activity = time.time()
        job = Job(self.request, data, channel, envelope)

        # Get the redis lock on the task so no other worker takes it
        _id = job.data['task_id']
        ts = TaskState(_id)
        res = await ts.acquire(timeout=120)
        if not res:
            raise Exception()

        # Add the task to the loop and start it
        task = self.loop.create_task(job())
        task._job = job
        job.task = task
        self._running.append(task)
        task.add_done_callback(self._done_callback)

    def _done_callback(self, task):
        """This is called when a job finishes execution"""
        self._done.append(task)
        self.total_run += 1

    async def start(self):
        """Called on worker startup. Connects to the rabbitmq. Declares and
        configures the different queues.

        """
        channel, transport, protocol = await amqp.get_connection()

        await channel.exchange_declare(
            exchange_name=app_settings['amqp']['exchange'],
            type_name='direct',
            durable=True)

        # Errored tasks will remain a limited period of time
        await channel.queue_declare(
            queue_name=app_settings['amqp']['queue'] + '-error', durable=True,
            arguments={
                'x-message-ttl': 1000 * 60 * 60 * 24 * 7  # 1 week
            })

        # Automatically move failed tasks to errored queue
        await channel.queue_declare(
            queue_name=app_settings['amqp']['queue'], durable=True,
            arguments={
                'x-dead-letter-exchange': app_settings['amqp']['exchange'],
                'x-dead-letter-routing-key': app_settings['amqp']['queue'] + '-error'
            })
        await channel.queue_bind(
            exchange_name=app_settings['amqp']['exchange'],
            queue_name=app_settings['amqp']['queue'],
            routing_key=app_settings['amqp']['queue'])

        await channel.basic_qos(prefetch_count=4)

        # Configure consume callback
        await channel.basic_consume(
            self.handle_queued_job,
            queue_name=app_settings['amqp']['queue'])

        # Start task that will update status periodically
        asyncio.ensure_future(self.update_status())

        logger.warning(f"Subscribed to queue: {app_settings['amqp']['queue']}")

    def cancel(self):
        """
        Cancels the worker (i.e: all its running tasks)
        """
        for task in self._running:
            task.cancel()

    async def join(self):
        """
        Waits for all tasks to finish
        """
        while len(self._running) > 0:
            await asyncio.sleep(0.01)

    async def is_cancelled(self, task_id):
        return True

    async def update_status(self):
        """Updates status for running tasks and kills running tasks that have
        been canceled.

        """
        while True:
            await asyncio.sleep(20)
            for task in self._running:
                _id = task._job.data['task_id']
                ts = TaskState(_id)

                if task in self._done:
                    # Clean-up running task list and release lock in
                    # StateManager
                    self._running.remove(task)
                    await ts.release()
                else:
                    # Get StateManager lock
                    result = await ts.acquire()
                    if not result:
                        # kill the task
                        task.cancel()

            # Cancel local tasks that have been cancelled in global
            # state manager
            async for val in self.state_manager.cancelation_list():
                for task in self._running:
                    _id = task._job.data['task_id']
                    if _id == val:
                        task.cancel()
                        await self._state_manager.clean_cancelled(_id)
