import sys
from bson.objectid import ObjectId, InvalidId
from girder.constants import AccessType
from girder.models.model_base import AccessControlledModel
from girder.models.model_base import ValidationException

import cumulus
from cumulus.taskflow import load_class
from girder.plugins.taskflow.models.taskflow import Taskflow as TaskflowModel

class QueueType(object):
    FIFO = 'fifo'
    LIFO = 'lifo'
    TYPES = [FIFO, LIFO]

class TaskStatus(object):
    PENDING = 'pending'
    RUNNING = 'running'

class Queue(AccessControlledModel):

    def initialize(self):
        self.name = 'queues'
        self.ensureIndices(['name'])

    def validate(self, queue):
        name = queue['name']
        userId = queue['userId']
        # Do we already have this name?
        if queue.get('_id') is None:
            if len(list(self.find(name=name, owner=userId, force=True))) > 0:
                raise ValidationException('"%s" has already been taken.' % name, field='name')
        return queue

    def find(self, name=None, owner=None, offset=0, limit=None, sort=None, user=None, force=False):
        query = {}

        if name is not None:
            query['name'] = name

        if owner is not None:
            if not isinstance(owner, ObjectId):
                try:
                    owner = ObjectId(owner)
                except InvalidId:
                    raise ValidationException('Invalid ObjectId: %s' % owner,
                                              field='owner')
            query['userId'] = owner

        cursor = super(Queue, self).find(query=query, sort=sort, user=user)

        if not force:
            for r in self.filterResultsByPermission(cursor=cursor, user=user,
                                                    level=AccessType.READ,
                                                    limit=limit, offset=offset):
                yield r
        else:
            for r  in cursor:
                yield r

    def create(self, name, type_, max_running, user=None):

        queue = {
            'name': name,
            'type': type_,
            'n_running': 0,
            'max_running': max_running,
            'pending': [],
            'taskflows': {}
        }

        userId = None
        if user is not None:
            userId = user['_id']

        queue['userId'] = userId

        self.setUserAccess(queue, user=user, level=AccessType.ADMIN)

        return self.save(queue)

    def add(self, queue, taskflow, params, user):
        query = {
            '_id': queue['_id'],
            'taskflows.%s' % taskflow['_id']: {
                '$exists': False
            }
        }

        payload = {
            'taskflowId': taskflow['_id'],
            'startParams': params
        }

        if queue['type'] == QueueType.FIFO:
            push = {
                'pending': payload
            }
        else:
            push = {
                'pending': {
                    '$each': [ payload ],
                    '$position': 0
                }
            }

        updates = {
            '$push': push,
            '$set': {
                'taskflows.%s' % taskflow['_id']: TaskStatus.PENDING
            }
        }
        self.update(query, updates)
        queue = self.load(queue['_id'], user=user, level=AccessType.READ)
        return queue

    def pop(self, queue, limit, user):
        queue, popped = self._pop_many(queue, limit, user)

        for task in popped:
            self._start_taskflow(task['taskflowId'], task['start_params'], user)

        return queue

    def finish(self, queue, taskflow, user):
        query = {
            '_id': queue['_id'],
            'taskflows.%s' % taskflow['_id']: TaskStatus.RUNNING
        }

        updates = {
            '$inc': {
                'n_running': -1
            },
            '$unset': {
                'taskflows.%s' % taskflow['_id']: ""
            }
        }

        self.update(query, updates)
        queue = self.load(queue['_id'], user=user, level=AccessType.READ)
        return queue

    def _pop_one(self, queue, user):
        max_running = queue['max_running']

        if max_running == 0:
            max_running = sys.maxsize

        pop_idx = -1
        if queue['type'] == QueueType.LIFO:
            pop_idx = 1

        query = {
            '_id': queue['_id'],
            'n_running': {
                '$lt': max_running
            },
            '$where': 'this.pending.length > 0'
        }

        updates = {
            '$inc': {
                'n_running': 1
            },
            '$pop': {
                'pending': -1
            }
        }

        # queue is the document BEFORE the updates
        queue = self.collection.find_one_and_update(query, updates)
        taskflow_id = None
        start_params = None

        if queue is None:
            return queue, taskflow_id, start_params

        n_running = queue['n_running']
        pending = queue['pending']
        if (n_running >= max_running or len(pending) == 0):
            return queue, taskflow_id, start_params

        task = pending.pop(0)
        taskflow_id = task['taskflowId']
        start_params = task['startParams']

        query = {
            '_id': queue['_id']
        }

        updates = {
            '$set': {
                'taskflows.%s' % taskflow_id: TaskStatus.RUNNING
            }
        }

        self.update(query, updates)
        queue = self.load(queue['_id'], user=user, level=AccessType.READ)

        return queue, taskflow_id, start_params

    def _pop_many(self, queue, limit, user):
        popped = []
        queue_, taskflow_id, start_params = self._pop_one(queue, user)
        while taskflow_id is not None and len(popped) < limit:
            queue = queue_
            popped.append({'taskflowId': taskflow_id, 'start_params': start_params})
            queue_, taskflow_id, start_params = self._pop_one(queue, user)

        return queue, popped

    def _start_taskflow(self, taskflow_id, params, user):
        taskflow = TaskflowModel().load(taskflow_id, user=user)

        constructor = load_class(taskflow['taskFlowClass'])
        token = self.model('token').createToken(user=user, days=7)

        workflow = constructor(
            id=str(taskflow['_id']),
            girder_token=token['_id'],
            girder_api_url=cumulus.config.girder.baseUrl
        )

        if params is None:
            params = {}

        workflow.start(**params)

        return workflow
