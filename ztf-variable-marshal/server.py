from aiohttp import web
import jinja2
import aiohttp_jinja2
from aiohttp_session import setup, get_session, session_middleware
from aiohttp_session.cookie_storage import EncryptedCookieStorage
import aiofiles
import json
import jwt
from motor.motor_asyncio import AsyncIOMotorClient
from bson.json_util import loads, dumps
import datetime
import time
from ast import literal_eval
from async_timeout import timeout
import asyncio
# import concurrent.futures
from misaka import Markdown, HtmlRenderer
import os
import pathlib
import shutil
import re
import numpy as np
import string
import random
import traceback
from penquins import Kowalski

from utils import *


''' markdown rendering '''
rndr = HtmlRenderer()
md = Markdown(rndr, extensions=('fenced-code',))


''' load config and secrets '''
with open('/app/config.json') as cjson:
    config = json.load(cjson)

with open('/app/secrets.json') as sjson:
    secrets = json.load(sjson)

for k in secrets:
    if k in config:
        config[k].update(secrets.get(k, {}))
    else:
        config[k] = secrets[k]
# print(config)


async def init_db():
    _client = AsyncIOMotorClient(username=config['database']['admin'],
                                 password=config['database']['admin_pwd'],
                                 host=config['database']['host'],
                                 port=config['database']['port'])

    # _id: db_name.user_name
    user_ids = []
    async for _u in _client.admin.system.users.find({}, {'_id': 1}):
        user_ids.append(_u['_id'])

    print(user_ids)

    db_name = config['database']['db']
    username = config['database']['user']

    # print(f'{db_name}.{username}')
    # print(user_ids)

    _mongo = _client[db_name]

    if f'{db_name}.{username}' not in user_ids:
        await _mongo.command('createUser', config['database']['user'],
                             pwd=config['database']['pwd'], roles=['readWrite'])
        print('Successfully initialized db')

    _mongo.client.close()


async def add_admin(_mongo):
    """
        Create admin user for the web interface if it does not exists already
    :return:
    """
    ex_admin = await _mongo.users.find_one({'_id': config['server']['admin_username']})
    if ex_admin is None or len(ex_admin) == 0:
        try:
            await _mongo.users.insert_one({'_id': config['server']['admin_username'],
                                           'password': generate_password_hash(config['server']['admin_password']),
                                           'permissions': {},
                                           'last_modified': utc_now()
                                           })
        except Exception as e:
            print(f'Got error: {str(e)}')
            _err = traceback.format_exc()
            print(_err)


routes = web.RouteTableDef()


@web.middleware
async def auth_middleware(request, handler):
    """
        auth middleware
    :param request:
    :param handler:
    :return:
    """
    tic = time.time()
    request.user = None
    jwt_token = request.headers.get('authorization', None)

    if jwt_token:
        try:
            payload = jwt.decode(jwt_token, request.app['JWT']['JWT_SECRET'],
                                 algorithms=[request.app['JWT']['JWT_ALGORITHM']])
            # print('Godny token!')
        except (jwt.DecodeError, jwt.ExpiredSignatureError):
            return web.json_response({'message': 'Token is invalid'}, status=400)

        request.user = payload['user_id']

    response = await handler(request)
    toc = time.time()
    # print(f"Auth middleware took {toc-tic} seconds to execute")

    return response


def auth_required(func):
    """
        Wrapper to ensure successful user authorization to use the API
    :param func:
    :return:
    """
    def wrapper(request):
        if not request.user:
            return web.json_response({'message': 'Auth required'}, status=401)
        return func(request)
    return wrapper


def login_required(func):
    """
        Wrapper to ensure successful user authorization to use the web frontend
    :param func:
    :return:
    """
    async def wrapper(request):
        # get session:
        session = await get_session(request)
        if 'jwt_token' not in session:
            # return web.json_response({'message': 'Auth required'}, status=401)
            # redirect to login page
            location = request.app.router['login'].url_for()
            # location = '/login'
            raise web.HTTPFound(location=location)
        else:
            jwt_token = session['jwt_token']
            if not await token_ok(request, jwt_token):
                # return web.json_response({'message': 'Auth required'}, status=401)
                # redirect to login page
                location = request.app.router['login'].url_for()
                # location = '/login'
                raise web.HTTPFound(location=location)
        return await func(request)
    return wrapper


async def token_ok(request, jwt_token):
    try:
        payload = jwt.decode(jwt_token, request.app['JWT']['JWT_SECRET'],
                             algorithms=[request.app['JWT']['JWT_ALGORITHM']])
        return True
    except (jwt.DecodeError, jwt.ExpiredSignatureError):
        return False


@routes.post('/auth')
async def auth(request):
    try:
        post_data = await request.json()
    except Exception as _e:
        print(f'Cannot extract json() from request, trying post(): {str(_e)}')
        # _err = traceback.format_exc()
        # print(_err)
        post_data = await request.post()

    # print(post_data)

    # must contain 'username' and 'password'

    if ('username' not in post_data) or (len(post_data['username']) == 0):
        return web.json_response({'message': 'Missing "username"'}, status=400)
    if ('password' not in post_data) or (len(post_data['password']) == 0):
        return web.json_response({'message': 'Missing "password"'}, status=400)

    username = str(post_data['username'])
    password = str(post_data['password'])

    try:
        # user exists and passwords match?
        select = await request.app['mongo'].users.find_one({'_id': username})
        if check_password_hash(select['password'], password):
            payload = {
                'user_id': username,
                'exp': datetime.datetime.utcnow() + datetime.timedelta(
                    seconds=request.app['JWT']['JWT_EXP_DELTA_SECONDS'])
            }
            jwt_token = jwt.encode(payload,
                                   request.app['JWT']['JWT_SECRET'],
                                   request.app['JWT']['JWT_ALGORITHM'])

            return web.json_response({'token': jwt_token.decode('utf-8')})

        else:
            return web.json_response({'message': 'Wrong credentials'}, status=400)

    except Exception as e:
        print(f'Got error: {str(e)}')
        _err = traceback.format_exc()
        print(_err)
        return web.json_response({'message': 'Wrong credentials'}, status=400)


@routes.get('/login')
async def login_get(request):
    """
        Serve login page
    :param request:
    :return:
    """
    context = {'logo': config['server']['logo']}
    response = aiohttp_jinja2.render_template('template-login.html',
                                              request,
                                              context)
    return response


@routes.post('/login', name='login')
async def login_post(request):
    """
        Server login page for the browser
    :param request:
    :return:
    """
    try:
        try:
            post_data = await request.json()
        except Exception as _e:
            print(f'Cannot extract json() from request, trying post(): {str(_e)}')
            # _err = traceback.format_exc()
            # print(_err)
            post_data = await request.post()

        # get session:
        session = await get_session(request)

        if ('username' not in post_data) or (len(post_data['username']) == 0):
            return web.json_response({'message': 'Missing "username"'}, status=400)
        if ('password' not in post_data) or (len(post_data['password']) == 0):
            return web.json_response({'message': 'Missing "password"'}, status=400)

        username = str(post_data['username'])
        password = str(post_data['password'])

        # print(username, password)
        print(f'User {username} logged in.')

        # user exists and passwords match?
        select = await request.app['mongo'].users.find_one({'_id': username})
        if check_password_hash(select['password'], password):
            payload = {
                'user_id': username,
                'exp': datetime.datetime.utcnow() + datetime.timedelta(
                    seconds=request.app['JWT']['JWT_EXP_DELTA_SECONDS'])
            }
            jwt_token = jwt.encode(payload,
                                   request.app['JWT']['JWT_SECRET'],
                                   request.app['JWT']['JWT_ALGORITHM'])

            # store the token, will need it
            session['jwt_token'] = jwt_token.decode('utf-8')
            session['user_id'] = username

            print('LOGIN', session)

            return web.json_response({'message': 'success'}, status=200)

        else:
            raise Exception('Bad credentials')

    except Exception as _e:
        print(f'Got error: {str(_e)}')
        _err = traceback.format_exc()
        print(_err)
        return web.json_response({'message': f'Failed to login user: {_err}'}, status=401)


@routes.get('/logout', name='logout')
async def logout(request):
    """
        Logout web user
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)

    session.invalidate()

    # redirect to login page
    location = request.app.router['login'].url_for()
    # location = '/login'
    raise web.HTTPFound(location=location)


@routes.get('/test')
@auth_required
async def handler_test(request):
    return web.json_response({'message': 'test ok.'}, status=200)


@routes.get('/test_wrapper')
@login_required
async def wrapper_handler_test(request):
    return web.json_response({'message': 'test ok.'}, status=200)


@routes.get('/', name='root')
@login_required
async def root_handler(request):
    """
        Serve home page for the browser
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)

    context = {'logo': config['server']['logo'],
               'user': session['user_id']}
    response = aiohttp_jinja2.render_template('template-root.html',
                                              request,
                                              context)
    # response.headers['Content-Language'] = 'ru'
    return response


''' manage users: API '''


@routes.get('/users')
@login_required
async def manage_users(request):
    """
        Serve users page for the browser
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)

    # only admin can access this
    if session['user_id'] == config['server']['admin_username']:
        users = await request.app['mongo'].users.find({}, {'password': 0}).to_list(length=1000)
        # print(users)

        context = {'logo': config['server']['logo'],
                   'user': session['user_id'],
                   'users': users}
        response = aiohttp_jinja2.render_template('template-users.html',
                                                  request,
                                                  context)
        return response

    else:
        return web.json_response({'message': '403 Forbidden'}, status=403)


@routes.put('/users')
@login_required
async def add_user(request):
    """
        Add new user to DB
    :return:
    """
    # get session:
    session = await get_session(request)

    _data = await request.json()
    # print(_data)

    if session['user_id'] == config['server']['admin_username']:
        try:
            username = _data['user'] if 'user' in _data else None
            password = _data['password'] if 'password' in _data else None
            permissions = _data['permissions'] if 'permissions' in _data else '{}'

            if len(username) == 0 or len(password) == 0:
                return web.json_response({'message': 'username and password must be set'}, status=500)

            if len(permissions) == 0:
                permissions = '{}'

            # add user to coll_usr collection:
            await request.app['mongo'].users.insert_one(
                {'_id': username,
                 'password': generate_password_hash(password),
                 'permissions': literal_eval(str(permissions)),
                 'last_modified': datetime.datetime.now()}
            )

            return web.json_response({'message': 'success'}, status=200)

        except Exception as _e:
            print(f'Got error: {str(_e)}')
            _err = traceback.format_exc()
            print(_err)
            return web.json_response({'message': f'Failed to add user: {_err}'}, status=500)
    else:
        return web.json_response({'message': '403 Forbidden'}, status=403)


@routes.delete('/users')
@login_required
async def remove_user(request):
    """
        Remove user from DB
    :return:
    """
    # get session:
    session = await get_session(request)

    _data = await request.json()
    # print(_data)

    if session['user_id'] == config['server']['admin_username']:
        try:
            # get username from request
            username = _data['user'] if 'user' in _data else None
            if username == config['server']['admin_username']:
                return web.json_response({'message': 'Cannot remove the superuser!'}, status=500)

            # try to remove the user:
            await request.app['mongo'].users.delete_one({'_id': username})

            return web.json_response({'message': 'success'}, status=200)

        except Exception as _e:
            print(f'Got error: {str(_e)}')
            _err = traceback.format_exc()
            print(_err)
            return web.json_response({'message': f'Failed to remove user: {_err}'}, status=500)
    else:
        return web.json_response({'message': '403 Forbidden'}, status=403)


@routes.post('/users')
@login_required
async def edit_user(request):
    """
        Edit user info
    :return:
    """
    # get session:
    session = await get_session(request)

    _data = await request.json()
    # print(_data)

    if session['user_id'] == config['server']['admin_username']:
        try:
            _id = _data['_user'] if '_user' in _data else None
            username = _data['edit-user'] if 'edit-user' in _data else None
            password = _data['edit-password'] if 'edit-password' in _data else None
            # permissions = _data['edit-permissions'] if 'edit-permissions' in _data else '{}'

            if _id == config['server']['admin_username'] and username != config['server']['admin_username']:
                return web.json_response({'message': 'Cannot change the admin username!'}, status=500)

            if len(username) == 0:
                return web.json_response({'message': 'username must be set'}, status=500)

            # change username:
            if _id != username:
                select = await request.app['mongo'].users.find_one({'_id': _id})
                select['_id'] = username
                await request.app['mongo'].users.insert_one(select)
                await request.app['mongo'].users.delete_one({'_id': _id})

            # change password:
            if len(password) != 0:
                await request.app['mongo'].users.update_one(
                    {'_id': username},
                    {
                        '$set': {
                            'password': generate_password_hash(password)
                        },
                        '$currentDate': {'last_modified': True}
                    }
                )

            # change permissions:
            # if len(permissions) != 0:
            #     select = await request.app['mongo'].users.find_one({'_id': username}, {'_id': 0, 'permissions': 1})
            #     # print(select)
            #     # print(permissions)
            #     _p = literal_eval(str(permissions))
            #     # print(_p)
            #     if str(permissions) != str(select['permissions']):
            #         result = await request.app['mongo'].users.update_one(
            #             {'_id': _id},
            #             {
            #                 '$set': {
            #                     'permissions': _p
            #                 },
            #                 '$currentDate': {'last_modified': True}
            #             }
            #         )

            return web.json_response({'message': 'success'}, status=200)

        except Exception as _e:
            print(f'Got error: {str(_e)}')
            _err = traceback.format_exc()
            print(_err)
            return web.json_response({'message': f'Failed to remove user: {_err}'}, status=500)
    else:
        return web.json_response({'message': '403 Forbidden'}, status=403)


''' query API'''


regex = dict()
regex['collection_main'] = re.compile(r"db\[['\"](.*?)['\"]\]")
regex['aggregate'] = re.compile(r"aggregate\((\[(?s:.*)\])")


def parse_query(task, save: bool=False):
    # save auxiliary stuff
    kwargs = task['kwargs'] if 'kwargs' in task else {}

    # reduce!
    task_reduced = {'user': task['user'], 'query': {}, 'kwargs': kwargs}

    if task['query_type'] == 'general_search':
        # specify task type:
        task_reduced['query_type'] = 'general_search'
        # nothing dubious to start with?
        if task['user'] != config['server']['admin_username']:
            go_on = True in [s in str(task['query']) for s in ['.aggregate(',
                                                               '.map_reduce(',
                                                               '.distinct(',
                                                               '.count_documents(',
                                                               '.index_information(',
                                                               '.find_one(',
                                                               '.find(']] and \
                    True not in [s in str(task['query']) for s in ['import',
                                                                   'pymongo.',
                                                                   'shutil.',
                                                                   'command(',
                                                                   'bulk_write(',
                                                                   'exec(',
                                                                   'spawn(',
                                                                   'subprocess(',
                                                                   'call(',
                                                                   'insert(',
                                                                   'update(',
                                                                   'delete(',
                                                                   'create_index(',
                                                                   'create_collection(',
                                                                   'run(',
                                                                   'popen(',
                                                                   'Popen(']] and \
                    str(task['query']).strip()[0] not in ('"', "'", '[', '(', '{', '\\')
        else:
            go_on = True

        # TODO: check access permissions:
        # TODO: for now, only check on admin stuff
        if task['user'] != config['server']['admin_username']:
            prohibited_collections = ('users', 'queries', 'stats')

            # get the main collection that is being queried:
            main_collection = regex['collection_main'].search(str(task['query'])).group(1)
            # print(main_collection)

            if main_collection in prohibited_collections:
                go_on = False

            # aggregating?
            if '.aggregate(' in str(task['query']):
                pipeline = literal_eval(regex['aggregate'].search(str(task['query'])).group(1))
                # pipeline = literal_eval(self.regex['aggregate'].search(str(task['query'])).group(1))
                lookups = [_ip for (_ip, _pp) in enumerate(pipeline) if '$lookup' in _pp]
                for _l in lookups:
                    if pipeline[_l]['$lookup']['from'] in prohibited_collections:
                        go_on = False

        if go_on:
            task_reduced['query'] = task['query']
        else:
            raise Exception('Atata!')

    elif task['query_type'] == 'cone_search':
        # specify task type:
        task_reduced['query_type'] = 'cone_search'
        # cone search radius:
        cone_search_radius = float(task['object_coordinates']['cone_search_radius'])
        # convert to rad:
        if task['object_coordinates']['cone_search_unit'] == 'arcsec':
            cone_search_radius *= np.pi / 180.0 / 3600.
        elif task['object_coordinates']['cone_search_unit'] == 'arcmin':
            cone_search_radius *= np.pi / 180.0 / 60.
        elif task['object_coordinates']['cone_search_unit'] == 'deg':
            cone_search_radius *= np.pi / 180.0
        elif task['object_coordinates']['cone_search_unit'] == 'rad':
            cone_search_radius *= 1
        else:
            raise Exception('Unknown cone search unit. Must be in [deg, rad, arcsec, arcmin]')

        for catalog in task['catalogs']:
            # TODO: check that not trying to query what's not allowed!
            task_reduced['query'][catalog] = dict()
            # parse catalog query:
            # construct filter
            _filter = task['catalogs'][catalog]['filter']
            if isinstance(_filter, str):
                # passed string? evaluate:
                catalog_query = literal_eval(_filter.strip())
            elif isinstance(_filter, dict):
                # passed dict?
                catalog_query = _filter
            else:
                raise ValueError('Unsupported filter specification')

            # construct projection
            # catalog_projection = dict()
            # FIXME: always return standardized coordinates?
            # catalog_projection = {'coordinates.epoch': 1, 'coordinates.radec_str': 1, 'coordinates.radec': 1}
            _projection = task['catalogs'][catalog]['projection']
            if isinstance(_projection, str):
                # passed string? evaluate:
                catalog_projection = literal_eval(_projection.strip())
            elif isinstance(_filter, dict):
                # passed dict?
                catalog_projection = _projection
            else:
                raise ValueError('Unsupported projection specification')

            # parse coordinate list
            # print(task['object_coordinates']['radec'])
            objects = literal_eval(task['object_coordinates']['radec'].strip())
            # print(type(objects), isinstance(objects, dict), isinstance(objects, list))

            # this could either be list [(ra1, dec1), (ra2, dec2), ..] or dict {'name': (ra1, dec1), ...}
            if isinstance(objects, list):
                object_coordinates = objects
                object_names = [str(obj_crd) for obj_crd in object_coordinates]
            elif isinstance(objects, dict):
                object_names, object_coordinates = zip(*objects.items())
                object_names = list(map(str, object_names))
            else:
                raise ValueError('Unsupported type of object coordinates')

            # print(object_names, object_coordinates)

            for oi, obj_crd in enumerate(object_coordinates):
                # convert ra/dec into GeoJSON-friendly format
                # print(obj_crd)
                _ra, _dec = radec_str2geojson(*obj_crd)
                # print(str(obj_crd), _ra, _dec)
                object_position_query = dict()
                object_position_query['coordinates.radec_geojson'] = {
                    '$geoWithin': {'$centerSphere': [[_ra, _dec], cone_search_radius]}}
                # use stringified object coordinates as dict keys and merge dicts with cat/obj queries:
                task_reduced['query'][catalog][object_names[oi]] = ({**object_position_query, **catalog_query},
                                                                    {**catalog_projection})

    if save:
        # print(task_reduced)
        # task_hashable = dumps(task)
        task_hashable = dumps(task_reduced)
        # compute hash for task. this is used as key in DB
        task_hash = compute_hash(task_hashable)

        # print({'user': task['user'], 'task_id': task_hash})

        # mark as enqueued in DB:
        t_stamp = utc_now()
        if 'query_expiration_interval' not in kwargs:
            # default expiration interval:
            t_expires = t_stamp + datetime.timedelta(days=int(config['misc']['query_expiration_interval']))
        else:
            # custom expiration interval:
            t_expires = t_stamp + datetime.timedelta(days=int(kwargs['query_expiration_interval']))

        # dump task_hashable to file, as potentially too big to store in mongo
        # save task:
        user_tmp_path = os.path.join(config['path']['path_queries'], task['user'])
        # print(user_tmp_path)
        # mkdir if necessary
        if not os.path.exists(user_tmp_path):
            os.makedirs(user_tmp_path)
        task_file = os.path.join(user_tmp_path, f'{task_hash}.task.json')

        with open(task_file, 'w') as f_task_file:
            f_task_file.write(dumps(task))

        task_doc = {'task_id': task_hash,
                    'user': task['user'],
                    'task': task_file,
                    'result': None,
                    'status': 'enqueued',
                    'created': t_stamp,
                    'expires': t_expires,
                    'last_modified': t_stamp}

        return task_hash, task_reduced, task_doc

    else:
        return '', task_reduced, {}


async def execute_query(mongo, task_hash, task_reduced, task_doc, save: bool = False):

    db = mongo

    if save:
        # mark query as enqueued:
        await db.queries.insert_one(task_doc)

    result = dict()
    query_result = dict()

    query = task_reduced

    try:

        # cone search:
        if query['query_type'] == 'cone_search':
            # iterate over catalogs as they represent
            for catalog in query['query']:
                query_result[catalog] = dict()
                # iterate over objects:
                for obj in query['query'][catalog]:
                    # project?
                    if len(query['query'][catalog][obj][1]) > 0:
                        _select = db[catalog].find(query['query'][catalog][obj][0],
                                                   query['query'][catalog][obj][1])
                    # return the whole documents by default
                    else:
                        _select = db[catalog].find(query['query'][catalog][obj][0])
                    # unfortunately, mongoDB does not allow to have dots in field names,
                    # thus replace with underscores
                    query_result[catalog][obj.replace('.', '_')] = await _select.to_list(length=None)

        elif query['query_type'] == 'general_search':
            # just evaluate. I know that's dangerous, but I'm checking things in broker.py
            qq = bytes(query['query'], 'utf-8').decode('unicode_escape')

            _select = eval(qq)
            # _select = eval(query['query'])
            # _select = literal_eval(qq)

            if ('.find_one(' in qq) or ('.count_documents(' in qq) or ('.index_information(' in qq):
                _select = await _select

            # make it look like json
            # print(list(_select))
            if isinstance(_select, int) or isinstance(_select, float) or \
                    isinstance(_select, list) or isinstance(_select, dict):
                query_result['query_result'] = _select
            else:
                query_result['query_result'] = await _select.to_list(length=None)

        result['user'] = query['user']
        result['status'] = 'done'
        result['kwargs'] = query['kwargs'] if 'kwargs' in query else {}

        if not save:
            # dump result back
            result['result_data'] = query_result

        else:
            # save task result:
            user_tmp_path = os.path.join(config['path']['path_queries'], query['user'])
            # print(user_tmp_path)
            # mkdir if necessary
            if not os.path.exists(user_tmp_path):
                os.makedirs(user_tmp_path)
            task_result_file = os.path.join(user_tmp_path, f'{task_hash}.result.json')

            # save location in db:
            result['result'] = task_result_file

            async with aiofiles.open(task_result_file, 'w') as f_task_result_file:
                task_result = dumps(query_result)
                await f_task_result_file.write(task_result)

        # print(task_hash, result)

        # db book-keeping:
        if save:
            # mark query as done:
            await db.queries.update_one({'user': query['user'], 'task_id': task_hash},
                                        {'$set': {'status': result['status'],
                                                  'last_modified': utc_now(),
                                                  'result': result['result']}}
                                        )

        # return task_hash, dumps(result)
        return task_hash, result

    except Exception as e:
        print(f'Got error: {str(e)}')
        _err = traceback.format_exc()
        print(_err)

        # book-keeping:
        if save:
            # save task result with error message:
            user_tmp_path = os.path.join(config['path']['path_queries'], query['user'])
            # print(user_tmp_path)
            # mkdir if necessary
            if not os.path.exists(user_tmp_path):
                os.makedirs(user_tmp_path)
            task_result_file = os.path.join(user_tmp_path, f'{task_hash}.result.json')

            # save location in db:
            result['user'] = query['user']
            result['status'] = 'failed'

            query_result = dict()
            query_result['msg'] = _err

            async with aiofiles.open(task_result_file, 'w') as f_task_result_file:
                task_result = dumps(query_result)
                await f_task_result_file.write(task_result)

            # mark query as failed:
            await db.queries.update_one({'user': query['user'], 'task_id': task_hash},
                                        {'$set': {'status': result['status'],
                                                  'last_modified': utc_now(),
                                                  'result': None}}
                                        )

        raise Exception('Query failed')


@routes.put('/query')
@login_required
async def query_handler(request):
    """
        Query own db, return json
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)
    user = session['user_id']

    try:
        _query = await request.json()
    except Exception as _e:
        print(f'Cannot extract json() from request, trying post(): {str(_e)}')
        _query = await request.post()
    # print(_query)

    # todo: parse and execute query awaiting the result

    try:
        # parse query
        known_query_types = ('cone_search', 'general_search')

        assert _query['query_type'] in known_query_types, \
            f'query_type {_query["query_type"]} not in {str(known_query_types)}'

        _query['user'] = user
        save = False  # never save to db when querying from the browser

        # tic = time.time()
        task_hash, task_reduced, task_doc = parse_query(_query, save=save)
        # toc = time.time()
        # print(f'parsing task took {toc-tic} seconds')
        # print(task_hash, task_reduced, task_doc)

        # schedule query execution:
        task_hash, result = await execute_query(request.app['mongo'], task_hash, task_reduced, task_doc, save)

        print(result)

        return web.json_response({'message': 'success', 'result': result}, status=200, dumps=dumps)

    except Exception as _e:
        print(f'Got error: {str(_e)}')
        _err = traceback.format_exc()
        print(_err)
        return web.json_response({'message': f'failure: {_err}'}, status=500)


''' sources API '''


@routes.get('/sources')
@login_required
async def sources_get_handler(request):
    """
        Serve saved sources page for the browser
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)

    # get last 10 added sources
    # sources = await request.app['mongo'].sources.find({},
    #                                                   {'_id': 1,
    #                                                    'ra': 1,
    #                                                    'dec': 1,
    #                                                    'p': 1,
    #                                                    'source_type': 1,
    #                                                    'created': 1}).limit(10).sort({'created': -1}).to_list(
    #     length=None)
    sources = await request.app['mongo'].sources.find({},
                                                      {'coordinates': 0,
                                                       'lc.data': 0}).limit(10).\
        sort([('created', -1)]).to_list(length=None)

    context = {'logo': config['server']['logo'],
               'user': session['user_id'],
               'data': sources,
               'messages': [['Displaying latest saved sources', 'info']]}

    response = aiohttp_jinja2.render_template('template-sources.html',
                                              request,
                                              context)
    return response


@routes.get('/sources/{source_id}')
@login_required
async def source_handler(request):
    """
        Serve single saved source page for the browser
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)

    _id = request.match_info['source_id']

    source = await request.app['mongo'].sources.find_one({'_id': _id})

    frmt = request.query.get('format', 'web')
    # print(frmt)

    if frmt == 'json':
        return web.json_response(source, status=200, dumps=dumps)

    # for the web, reformat/compute data fields:
    for lc in source['lc']:
        if lc['lc_type'] == 'temporal':
            mags = np.array([llc['mag'] for llc in lc['data']])
            magerrs = np.array([llc['magerr'] for llc in lc['data']])
            mjds = np.array([llc['mjd'] for llc in lc['data']])
            hjds = np.array([llc['hjd'] for llc in lc['data']])
            datetimes = np.array([mjd_to_datetime(llc['mjd']).strftime('%Y-%m-%d %H:%M:%S') for llc in lc['data']])

            ind_sort = np.argsort(mjds)
            mags = mags[ind_sort].tolist()
            magerrs = magerrs[ind_sort].tolist()
            mjds = mjds[ind_sort].tolist()
            datetimes = datetimes[ind_sort].tolist()

            lc.pop('data', None)
            lc['mag'] = mags
            lc['magerr'] = magerrs
            lc['mjd'] = mjds
            lc['hjd'] = hjds
            lc['dt'] = datetimes

    context = {'logo': config['server']['logo'],
               'user': session['user_id'],
               'source': source}
    response = aiohttp_jinja2.render_template('template-source.html',
                                              request,
                                              context)
    return response


@routes.put('/sources')
@login_required
async def sources_put_handler(request):
    """
        Save ZTF source to own db
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)

    try:
        _r = await request.json()
    except Exception as _e:
        print(f'Cannot extract json() from request, trying post(): {str(_e)}')
        _r = await request.post()
    # print(_r)

    try:
        assert '_id' in _r, '_id not specified'

        kowalski_query = {"query_type": "general_search",
                          "query": f"db['{config['kowalski']['coll_sources']}'].find({{'_id': {_r['_id']}}}, " +
                                   f"{{'_id': 1, 'ra': 1, 'dec': 1, 'filter': 1, 'coordinates': 1, 'data': 1}})"
                          }

        resp = request.app['kowalski'].query(kowalski_query)
        ztf_source = resp['result_data']['query_result'][0]

        # build doc to ingest:
        doc = dict()

        source_id_base = \
            f'ZTFS{datetime.datetime.utcnow().strftime("%y")}{ztf_source["coordinates"]["radec_str"][0][:2]}'

        num_saved_sources = await request.app['mongo'].sources.count_documents({'_id':
                                                                                    {'$regex': f'{source_id_base}.*'}})
        # postfix = num2alphabet(num_saved_sources + 1)
        if num_saved_sources > 0:
            saved_source_ids = await request.app['mongo'].sources.find({'_id': {'$regex': f'{source_id_base}.*'}},
                                                                       {'_id': 1}).to_list(length=None)
            saved_source_ids = [s['_id'] for s in saved_source_ids]
            # print(saved_source_ids)
            num_last = alphabet2num(sorted(saved_source_ids)[-1][8:])

            postfix = num2alphabet(num_last + 1)

        else:
            postfix = 'a'

        source_id = source_id_base + postfix

        doc['_id'] = source_id
        doc['ra'] = ztf_source['ra']
        doc['dec'] = ztf_source['dec']
        doc['coordinates'] = ztf_source['coordinates']
        # [{'period': float, 'period_error': float}]:
        doc['p'] = []
        doc['source_type'] = []
        # temporal, folded; if folded - 'p': [{'period': float, 'period_error': float}]
        lc = {'telescope': 'PO:1.2m',
              'instrument': 'ZTF',
              'id': ztf_source['_id'],
              'filter': ztf_source['filter'],
              'lc_type': 'temporal',
              'data': ztf_source['data']}
        doc['lc'] = [lc]

        doc['created'] = utc_now()

        await request.app['mongo'].sources.insert_one(doc)

        return web.json_response({'message': 'success'}, status=200)

    except Exception as _e:
        print(f'Failed to ingest source: {str(_e)}')

        return web.json_response({'message': f'ingestion failed {str(_e)}'}, status=500)


''' search ZTF light curve db '''


@routes.get('/search')
@login_required
async def search_get_handler(request):
    """
        Serve GS page for the browser
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)

    context = {'logo': config['server']['logo'],
               'user': session['user_id']}
    response = aiohttp_jinja2.render_template('template-search.html',
                                              request,
                                              context)
    return response


@routes.post('/search')
@login_required
async def search_post_handler(request):
    """
        Process Kowalski query
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)

    try:
        _query = await request.json()
    except Exception as _e:
        print(f'Cannot extract json() from request, trying post(): {str(_e)}')
        # _err = traceback.format_exc()
        # print(_err)
        _query = await request.post()
    print(_query)

    try:
        # convert to Kowalski query and execute

        kowalski_query = {"query_type": "cone_search",
                          "object_coordinates": {
                              "radec": _query['radec'],
                              "cone_search_radius": _query['cone_search_radius'],
                              "cone_search_unit": _query['cone_search_unit']
                          },
                          "catalogs": {
                              config['kowalski']['coll_sources']: {
                                  "filter": _query['filter'] if len(_query['filter']) > 0 else "{}",
                                  "projection": "{'_id': 1, 'ra': 1, 'dec': 1, 'magrms': 1, 'maxmag': 1," +
                                                "'vonneumannratio': 1, 'filter': 1," +
                                                "'maxslope': 1, 'meanmag': 1, 'medianabsdev': 1," +
                                                "'medianmag': 1, 'minmag': 1, 'ngoodobs': 1," +
                                                "'nobs': 1, 'refmag': 1, 'iqr': 1, " +
                                                "'data.mag': 1, 'data.magerr': 1, 'data.mjd': 1, 'coordinates': 1}"
                              }
                          }
                          }

        resp = request.app['kowalski'].query(kowalski_query)
        # print(resp)

        pos_key = list(resp['result_data'][config['kowalski']['coll_sources']].keys())[0]
        data = resp['result_data'][config['kowalski']['coll_sources']][pos_key]

        # re-format data (mjd, mag, magerr) for easier previews in the browser:
        for source in data:
            lc = source['data']
            # print(lc)
            mags = np.array([llc['mag'] for llc in lc])
            magerrs = np.array([llc['magerr'] for llc in lc])
            mjds = np.array([llc['mjd'] for llc in lc])
            datetimes = np.array([mjd_to_datetime(llc['mjd']).strftime('%Y-%m-%d %H:%M:%S') for llc in lc])

            ind_sort = np.argsort(mjds)
            mags = mags[ind_sort].tolist()
            magerrs = magerrs[ind_sort].tolist()
            mjds = mjds[ind_sort].tolist()
            datetimes = datetimes[ind_sort].tolist()

            source.pop('data', None)
            source['mag'] = mags
            source['magerr'] = magerrs
            # source['mjd'] = mjds
            source['mjd'] = datetimes

        # print(data)

        context = {'logo': config['server']['logo'],
                   'user': session['user_id'],
                   'data': data,
                   'form': _query}
        response = aiohttp_jinja2.render_template('template-search.html',
                                                  request,
                                                  context)
        return response

    except Exception as _e:
        print(f'Querying Kowalski failed: {str(_e)}')

        context = {'logo': config['server']['logo'],
                   'user': session['user_id'],
                   'messages': [[str(_e), 'danger']]}
        response = aiohttp_jinja2.render_template('template-search.html',
                                                  request,
                                                  context)
        return response


''' web endpoints '''


@routes.get('/docs')
@login_required
async def docs_handler(request):
    """
        Serve docs page for the browser
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)

    # todo?

    context = {'logo': config['server']['logo'],
               'user': session['user_id']}
    response = aiohttp_jinja2.render_template('template-docs.html',
                                              request,
                                              context)
    return response


@routes.get('/docs/{doc}')
@login_required
async def doc_handler(request):
    """
        Serve doc page for the browser
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)

    doc = request.match_info['doc']

    title = doc.replace('_', ' ').capitalize()

    # render doc with misaka
    with open(os.path.join(config['path']['path_docs'],
                           doc + '.md'), 'r') as f:
        tut = f.read()

    content = md(tut)

    context = {'logo': config['server']['logo'],
               'user': session['user_id'],
               'title': title,
               'content': content}
    response = aiohttp_jinja2.render_template('template-doc.html',
                                              request,
                                              context)
    return response


async def app_factory():
    """
        App Factory
    :return:
    """

    # init db if necessary
    await init_db()

    # Database connection
    client = AsyncIOMotorClient(f"mongodb://{config['database']['user']}:{config['database']['pwd']}@" +
                                f"{config['database']['host']}:{config['database']['port']}/{config['database']['db']}")
    mongo = client[config['database']['db']]

    # add site admin if necessary
    await add_admin(mongo)

    # init app with auth middleware
    app = web.Application(middlewares=[auth_middleware])

    # store mongo connection
    app['mongo'] = mongo

    # indices
    await app['mongo'].sources.create_index([('coordinates.radec_geojson', '2dsphere'),
                                             ('_id', 1)], background=True)
    await app['mongo'].sources.create_index([('created', -1)], background=True)
    await app['mongo'].sources.create_index([('lc.id', 1)], background=True)

    # graciously close mongo client on shutdown
    async def close_mongo(app):
        app['mongo'].client.close()

    app.on_cleanup.append(close_mongo)

    # Kowalski connection:
    app['kowalski'] = Kowalski(username=config['kowalski']['username'], password=config['kowalski']['password'])

    # set up JWT for user authentication/authorization
    app['JWT'] = {'JWT_SECRET': config['server']['JWT_SECRET_KEY'],
                  'JWT_ALGORITHM': 'HS256',
                  'JWT_EXP_DELTA_SECONDS': 30 * 86400 * 3}

    # render templates with jinja2
    aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader('./templates'))

    # set up browser sessions
    fernet_key = config['misc']['fernet_key'].encode()
    secret_key = base64.urlsafe_b64decode(fernet_key)
    setup(app, EncryptedCookieStorage(secret_key))

    # route table
    # app.add_routes([web.get('/', hello)])
    app.add_routes(routes)

    # static files
    app.add_routes([web.static('/static', './static')])

    # data files
    app.add_routes([web.static('/data', '/data')])

    return app


''' Tests '''


class TestAPIs(object):
    # python -m pytest -s server.py
    # python -m pytest server.py

    # test user management API for admin
    async def test_users(self, aiohttp_client):
        client = await aiohttp_client(await app_factory(_config=config))

        login = await client.post('/login', json={"username": config['server']['admin_username'],
                                                  "password": config['server']['admin_password']})
        # print(login)
        assert login.status == 200

        # test = await client.get('/lab/ztf-alerts')
        # print(test)

        # adding a user
        resp = await client.put('/users', json={'user': 'test_user', 'password': random_alphanumeric_str(6)})
        assert resp.status == 200
        # text = await resp.text()
        # text = await resp.json()

        # editing user credentials
        resp = await client.post('/users', json={'_user': 'test_user',
                                                 'edit-user': 'test_user',
                                                 'edit-password': random_alphanumeric_str(6)})
        assert resp.status == 200
        resp = await client.post('/users', json={'_user': 'test_user',
                                                 'edit-user': 'test_user_edited',
                                                 'edit-password': ''})
        assert resp.status == 200

        # deleting a user
        resp = await client.delete('/users', json={'user': 'test_user_edited'})
        assert resp.status == 200

    # test programmatic query API
    async def test_query(self, aiohttp_client):
        # todo:
        client = await aiohttp_client(await app_factory(_config=config))

        # check JWT authorization
        auth = await client.post(f'/auth',
                                 json={"username": config['server']['admin_username'],
                                       "password": config['server']['admin_password']})
        assert auth.status == 200
        # print(await auth.text())
        # print(await auth.json())
        credentials = await auth.json()
        assert 'token' in credentials

        access_token = credentials['token']

        headers = {'Authorization': access_token}

        collection = 'ZTF_alerts'

        # check query without book-keeping
        qu = {"query_type": "general_search",
              "query": f"db['{collection}'].find_one({{}}, {{'_id': 1}})",
              "kwargs": {"save": False}
              }
        # print(qu)
        resp = await client.put('/query', json=qu, headers=headers, timeout=1)
        assert resp.status == 200
        result = await resp.json()
        assert result['status'] == 'done'

        # check query with book-keeping
        qu = {"query_type": "general_search",
              "query": f"db['{collection}'].find_one({{}}, {{'_id': 1}})",
              "kwargs": {"enqueue_only": True, "_id": random_alphanumeric_str(32)}
              }
        # print(qu)
        resp = await client.put('/query', json=qu, headers=headers, timeout=0.15)
        assert resp.status == 200
        result = await resp.json()
        # print(result)
        assert result['status'] == 'enqueued'

        # remove enqueued query
        resp = await client.delete('/query', json={'task_id': result['query_id']}, headers=headers, timeout=1)
        assert resp.status == 200
        result = await resp.json()
        assert result['message'] == 'success'


if __name__ == '__main__':

    web.run_app(app_factory(), port=config['server']['port'])