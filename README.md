# POSM OpenDroneMap API

This is the [POSM](https://github.com/AmericanRedCross/posm) [OpenDroneMap](https://github.com/OpenDroneMap/OpenDroneMap) API. It does a few things:

* ingests JPEGs and PNGs shot from cameras transported by UAVs
* generates 3D models and orthographic photos using OpenDroneMap

## Building

```bash
docker build --rm -t posm-opendronemap-api .
```

See [Developing](#Developing) for additional instructions.

## Running

First, start `redis-server` so that Docker containers can access it (so that Celery can use it as a
broker and result backend and so that Flask-Tus can track uploads):

```bash
redis-server --bind 0.0.0.0
```

This runs `gunicorn` under the hood (using the default Docker `ENTRYPOINT`).

```bash
# get the host IP on OS X (wired, then wireless)
ip=$(ipconfig getifaddr en0 || ipconfig getifaddr en1)
docker run \
  -it \
  --rm \
  -e WEB_CONCURRENCY=$(nproc) \
  -e REDIS_URL="redis://${ip}/" \
  -v $(pwd)/projects:/app/projects \
  -v $(pwd)/uploads:/app/uploads \
  -p 8000:8000 \
  posm-opendronemap-api
```

This overrides the default `ENTRYPOINT` and starts the Celery daemon to run workers instead. Note
that the `projects` and `uploads` volumes are shared between containers.

```bash
# get the host IP on OS X (wired, then wireless)
ip=$(ipconfig getifaddr en0 || ipconfig getifaddr en1)
docker run \
  -it \
  --rm \
  -e REDIS_URL="redis://${ip}/" \
  -v $(pwd)/projects:/app/projects \
  -v $(pwd)/uploads:/app/uploads \
  --entrypoint celery \
  posm-opendronemap-api \
  worker \
  -A app.celery \
  --loglevel=info
```

## Developing

You can either run a development copy with `docker-compose`:

```bash
docker-compose up
```

...or you can develop against a local copy. To set up, create a `virtualenv`, install the
dependencies, and start up the API server and Celery workers. You'll also need a local Redis server.

Create a `virtualenv` and install dependencies:

```bash
virtualenv venv
source venv/bin/activate
pip install -Ur requirements.txt
npm install
```

Start the API server:

```bash
source venv/bin/activate
python app.py
```

Start the Celery workers:

```bash
source venv/bin/activate
venv/bin/celery worker -A app.celery --loglevel=info
```

To start Redis:

```bash
redis-server
```

## API Endpoints

To see an up-to-date list of API endpoints (and supported methods), run `python manage.py
list_routes`.

* `GET /projects` - List available projects.
* `GET /projects/<id>` - Get metadata for a project.
* `GET /projects/<id>/artifacts` - List available artifacts.
* `GET /projects/<id>/artifacts/<artifact id>` - Download a specific artifact.
* `GET /projects/<id>/images` - List available source images.
* `GET /projects/<id>/images/<image id>` - Download a specific source image.
* `GET /projects/<id>/images/<image id>/thumb` - Get a thumbnail for a specific image.
* `POST /projects/<id>/process` - Request creation of OpenDroneMap artifacts.
* `DELETE /projects/<id>/process` - Cancel a pending task.
* `GET /projects/<id>/status` - Check on the status of artifact creation.
* `PUT /projects/<id>/upload` - Upload imagery. Requires the image to be the `file` value of a
  `multipart/form-data` payload. E.g., `curl -X PUT -F "file=@image.jpg"
  http://localhost:8000/projects/<id>/upload`
* `POST /projects/upload` - [tus.io](https://tus.io/) upload endpoint.
* `GET /tasks` - List running tasks (raw).

## Environment Variables

* `APPLICATION_ROOT` - Optional application prefix. Defaults to ``.
* `DEBUG` - Enable Flask's debug mode. Defaults to `False`.
* `PROJECTS_PATH` - Where to store projects on the filesystem. Must be accessible by both the API
  server and Celery workers. Defaults to `projects` (relative to the current working directory).
* `UPLOADED_IMAGERY_DEST` - Where to (temporarily) store uploaded imagery. Must be accessible by
  both the API server and Celery workers. Defaults to `uploads/` (relative to the current working
  directory).
* `CELERY_BROKER_URL` - Celery broker URL. Defaults to the value of `REDIS_URL`.
* `CELERY_RESULT_BACKEND` - Celery result backend URL. Defaults to the value of `REDIS_URL`.
* `REDIS_URL` - Flask-Tus backend. Defaults to `redis://` (`localhost`, default port, default
  database).
* `USE_X_SENDFILE` - Whether Flask should use `X-Sendfile` to defer file serving to a proxying web
  server (this requires that the web server and API server are running on the same "server", so
  Docker won't work). Defaults to `False`.
