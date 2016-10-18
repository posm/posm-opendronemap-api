# coding=utf-8

from datetime import datetime
import json
import shutil
from StringIO import StringIO
import subprocess32 as subprocess
import os
import uuid

from cachetools.func import lru_cache
from celery import Celery
from flask import Flask, redirect, request, send_from_directory, jsonify, url_for
from flask_cors import CORS
from flask_uploads import UploadSet, configure_uploads
from flask_tus import tus_manager
import rasterio
from rasterio.warp import transform_bounds
from PIL import Image
from werkzeug.wsgi import DispatcherMiddleware


APPLICATION_ROOT = os.environ.get('APPLICATION_ROOT', '')
REDIS_URL = os.environ.get('REDIS_URL', 'redis://')
CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', REDIS_URL)
CELERY_DEFAULT_QUEUE = os.environ.get('CELERY_DEFAULT_QUEUE', 'posm-opendronemap-api')
CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', REDIS_URL)
PROJECTS_PATH = os.environ.get('PROJECTS_PATH', 'projects')
USE_X_SENDFILE = os.environ.get('USE_X_SENDFILE', False)
UPLOADED_IMAGERY_DEST = os.environ.get('UPLOADED_IMAGERY_DEST', 'uploads/')

# strip trailing slash if necessary
if PROJECTS_PATH[-1] == '/':
    PROJECTS_PATH = PROJECTS_PATH[:-1]

# add trailing slash if necessary
if UPLOADED_IMAGERY_DEST[-1] != '/':
    UPLOADED_IMAGERY_DEST = UPLOADED_IMAGERY_DEST[:-1]

app = Flask('posm-opendronemap-api')
CORS(app)
app.config['APPLICATION_ROOT'] = APPLICATION_ROOT
app.config['CELERY_BROKER_URL'] = CELERY_BROKER_URL
app.config['CELERY_DEFAULT_QUEUE'] = CELERY_DEFAULT_QUEUE
app.config['CELERY_RESULT_BACKEND'] = CELERY_RESULT_BACKEND
app.config['CELERY_TRACK_STARTED'] = True
app.config['USE_X_SENDFILE'] = USE_X_SENDFILE
app.config['UPLOADED_IMAGERY_DEST'] = UPLOADED_IMAGERY_DEST

# Initialize Celery
celery = Celery(app.name, broker=app.config['CELERY_BROKER_URL'])
celery.conf.update(app.config)

# Initialize Tus
# TODO upload to a specific project id
tm = tus_manager(app, upload_url='/projects/upload',
    upload_folder=app.config['UPLOADED_IMAGERY_DEST'])

# Initialize Flask-Uploads
imagery = UploadSet('imagery', ('jpg', 'png'))
configure_uploads(app, (imagery,))


@tm.upload_file_handler
def upload_file_handler(upload_file_path, id=None, filename=None):
    if filename is None:
        filename = os.path.basename(upload_file_path)

    if id is None:
        id = str(uuid.uuid4())

    images_path = os.path.join(PROJECTS_PATH, id, 'images')

    if not os.path.exists(images_path):
        os.makedirs(images_path)

    shutil.move(upload_file_path, os.path.join(images_path, filename))

    return os.path.join(id, 'images', filename)


@celery.task(bind=True)
def process_project(self, id):
    started_at = datetime.utcnow()
    project_path = os.path.join(PROJECTS_PATH, id)

    command = [
        'python',
        '/code/run.py',
        '--project-path',
        '.', # this will be executed from the project directory
    ]

    def cleanup():
        for dir in ('images_resize', 'odm_georeferencing', 'odm_meshing', 'odm_orthophoto', 'odm_texturing', 'opensfm', 'pmvs'):
            target_path = os.path.join(project_path, dir)
            os.path.isdir(target_path) and shutil.rmtree(target_path)
            os.path.isfile(target_path) and os.unlink(target_path)

    self.update_state(state='RUNNING',
                      meta={
                        'name': 'opendronemap',
                        'started_at': started_at.isoformat(),
                        'status': 'Processing imagery',
                        'task_id': self.request.id,
                      })

    child = None

    try:
        # start by cleaning up in case the previous run was cancelled
        cleanup()
        log_path = os.path.join(project_path, 'logs')

        os.path.exists(log_path) or os.mkdir(log_path)
        with open(os.path.join(log_path, 'stdout.log'), 'w+') as stdout:
            with open(os.path.join(log_path, 'stderr.log'), 'w+') as stderr:
                # NOTE: this is used instead of check_call so that we can call terminate() on the
                # child rather than assuming that signals will be passed through and be handled
                # correctly
                child = subprocess.Popen(command, cwd=project_path, stdout=stdout, stderr=stderr)
                child.wait(timeout=60*60*6)
    except subprocess.TimeoutExpired as e:
        child.kill()
        child.wait()
        cleanup()

        raise Exception(json.dumps({
            'name': 'opendronemap',
            'started_at': started_at.isoformat(),
            'command': ' '.join(command),
            'return_code': returncode,
            'status': 'Timed out'
        }))
    except subprocess.CalledProcessError as e:
        cleanup()

        raise Exception(json.dumps({
            'name': 'opendronemap',
            'started_at': started_at.isoformat(),
            'command': e.cmd,
            'return_code': e.returncode,
            'status': 'Failed'
        }))
    except:
        if child:
            child.terminate()
        raise

    # clean up and move artifacts
    artifacts_path = os.path.join(project_path, 'artifacts')
    if os.path.exists(artifacts_path):
        shutil.rmtree(artifacts_path)
    else:
        os.mkdir(artifacts_path)

    for artifact in ('odm_texturing', 'odm_orthophoto/odm_orthophoto.tif', 'odm_orthophoto/odm_orthophoto.png'):
        src_path = os.path.join(project_path, artifact)

        if os.path.isdir(src_path):
            for item in os.listdir(src_path):
                shutil.move(os.path.join(src_path, item), artifacts_path)
        else:
            os.path.exists(src_path) and shutil.move(src_path, artifacts_path)

    # create a thumbnail
    im = Image.open(os.path.join(artifacts_path, 'odm_orthophoto.png'))
    im.thumbnail((128, 128))
    im.save(os.path.join(artifacts_path, 'ortho_thumb.png'))

    with rasterio.drivers():
        with rasterio.open(os.path.join(artifacts_path, 'odm_orthophoto.tif')) as src:
            width = src.width
            height = src.height
            res = src.res

            metadata = get_metadata(id)

            metadata.update({
                'status': {
                    'state': 'SUCCESS',
                },
                'meta': {
                    'width': src.width,
                    'height': src.height,
                    'resolution': src.res,
                    'crs': str(src.crs),
                    'crs_wkt': src.crs.wkt,
                    'bounds': transform_bounds(src.crs, {'init': 'epsg:4326'}, *src.bounds),
                    'size': os.stat(src.name).st_size,
                }
            })

            save_metadata(id, metadata)

    cleanup()
    os.unlink(os.path.join(project_path, "process.task"))

    return {
        'name': 'preprocess',
        'completed_at': datetime.utcnow().isoformat(),
        'started_at': started_at,
        'status': 'Image processing completed'
    }


def get_task_status(id):
    task_info_path = os.path.join(PROJECTS_PATH, id, 'process.task')

    if os.path.exists(task_info_path):
        with open(task_info_path) as t:
            task_id = t.read()

        return fetch_status(task_id)

    else:
        return {}


def get_metadata(id):
    metadata_path = os.path.join(PROJECTS_PATH, id, 'index.json')
    images_path = os.path.join(PROJECTS_PATH, id, 'images')
    artifacts_path = os.path.join(PROJECTS_PATH, id, 'artifacts')

    if os.path.exists(metadata_path):
        with open(metadata_path) as metadata:
            metadata = json.load(metadata)
    else:
        metadata = {
            'images': [],
            'artifacts': [],
            'status': {},
            'user': {},
        }

    if os.path.exists(images_path):
        metadata['images'] = os.listdir(images_path)

    if os.path.exists(artifacts_path):
        metadata['artifacts'] = os.listdir(artifacts_path)

    status = get_task_status(id)
    if status:
        metadata['status'] = status

    return metadata


def save_metadata(id, metadata):
    with open(os.path.join(PROJECTS_PATH, id, 'index.json'), 'w') as metadata_file:
        metadata_file.write(json.dumps(metadata))


@app.errorhandler(IOError)
def handle_ioerror(error):
    return '', 404


@app.route('/tasks')
def list_tasks():
    i = celery.control.inspect()

    status = {
        'scheduled': i.scheduled(),
        'active': i.active(),
        'reserved': i.reserved(),
    }

    return jsonify(status), 200


@app.route('/projects')
def list_projects():
    """List available projects"""
    projects = dict(map(lambda project: (project, get_metadata(project)), filter(
        lambda project: os.path.isdir(os.path.join(PROJECTS_PATH, project)), os.listdir(PROJECTS_PATH))))

    return jsonify(projects), 200


@app.route('/projects', methods=['PUT'])
def create_project(id):
    body = request.get_json(force=True)

    id = str(uuid.uuid4())

    metadata = get_metadata(id)

    metadata['user'] = body

    save_metadata(id, metadata)

    return jsonify(metadata), 201


@app.route('/projects/<id>', methods=['PATCH', 'POST'])
def update_project(id):
    body = request.get_json(force=True)

    metadata = get_metadata(id)

    if request.method == 'PATCH':
        metadata['user'].update(body)
    else:
        metadata['user'] = body

    save_metadata(id, metadata)

    return jsonify(metadata), 200


@app.route('/projects/<id>/upload', methods=['PUT'])
def upload_imagery(id):
    path = app.config['UPLOADED_IMAGERY_DEST'] + imagery.save(request.files['file'])

    target_path = upload_file_handler(path, id=id)

    with app.app_context():
        return jsonify({
            'project': url_for('get_project', id=id, _external=True),
        }), 200


@app.route('/projects/<id>')
def get_project(id):
    return jsonify(get_metadata(id)), 200


@app.route('/projects/<id>/images')
def list_project_images(id):
    return jsonify(get_metadata(id)['images']), 200


@app.route('/projects/<id>/images/<image_id>')
def download_project_image(id, image_id):
    images_path = os.path.join(PROJECTS_PATH, id, 'images')
    return send_from_directory(
        images_path,
        image_id,
        conditional=True
    )


@app.route('/projects/<id>/images/<image_id>/thumb')
@lru_cache()
def get_project_image_thumbnail(id, image_id):
    im = Image.open(os.path.join(PROJECTS_PATH, id, 'images', image_id))
    out = StringIO()
    im.thumbnail((128, 128))
    im.save(out, "jpeg")

    return out.getvalue(), 200, {
        'Content-Type': 'image/jpeg'
    }


@app.route('/projects/<id>/logs/stderr')
def get_project_stderr(id):
    return send_from_directory(
        os.path.join(PROJECTS_PATH, id, 'logs'),
        'stderr.log',
        conditional=True,
        mimetype='text/plain',
    )


@app.route('/projects/<id>/logs/stdout')
def get_project_stdout(id):
    return send_from_directory(
        os.path.join(PROJECTS_PATH, id, 'logs'),
        'stdout.log',
        conditional=True,
        mimetype='text/plain',
    )


@app.route('/projects/<id>/artifacts')
def list_project_artifacts(id):
    return jsonify(get_metadata(id)['artifacts']), 200


@app.route('/projects/<id>/artifacts/<artifact_id>')
def download_project_artifact(id, artifact_id):
    return send_from_directory(
        os.path.join(PROJECTS_PATH, id, 'artifacts'),
        artifact_id,
        conditional=True
    )


@app.route('/projects/<id>/process', methods=['POST'])
def start_processing_project(id):
    task_info = os.path.join(PROJECTS_PATH, id, 'process.task')

    if os.path.exists(task_info) and not request.args.get('force'):
        return jsonify({
            'message': 'Processing already in progress, ?force=true to force'
        }), 400

    task = process_project.s(id=id).apply_async()

    # stash task.id so we know which task to look up
    with open(task_info, 'w') as f:
        f.write(task.id)

    return '', 202, {
        'Location': url_for('get_project_status', id=id)
    }


@app.route('/projects/<id>/process', methods=['DELETE'])
def cancel_processing_project(id):
    task_info = os.path.join(PROJECTS_PATH, id, 'process.task')

    with open(task_info) as t:
        task_id = t.read()

    celery.control.revoke(task_id, terminate=True)

    return '', 201


def fetch_status(task_id):
    result = celery.AsyncResult(task_id)

    status = {
        # TODO result.state doesn't account for the states of all children
        'state': result.state,
        'steps': []
    }

    for _, node in result.iterdeps(intermediate=True):
        if hasattr(node, 'info'):
            if isinstance(node.info, Exception):
                try:
                    status['steps'].append(json.loads(node.info.message))
                except:
                    status['steps'].append(node.info.message)
            else:
                status['steps'].append(node.info)

    return status


@app.route('/projects/<id>/status')
def get_project_status(id):
    task_info = os.path.join(PROJECTS_PATH, id, 'process.task')

    if os.path.exists(task_info):
        with open(task_info) as t:
            task_id = t.read()

        return jsonify(fetch_status(task_id)), 200

    elif os.path.exists(os.path.dirname(task_info)):
        metadata = get_metadata(id)
        return jsonify(metadata['status']), 200

    else:
        return '', 404


app.wsgi_app = DispatcherMiddleware(None, {
    app.config['APPLICATION_ROOT']: app.wsgi_app
})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)
