# -*- coding: utf-8 -*-
# Copyright 2014 OpenMarket Ltd
# Copyright 2019 New Vector Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from six.moves import configparser

import json
import logging
import sys
import threading
from logging.handlers import WatchedFileHandler

import flask
from flask import Flask, request

import prometheus_client
from prometheus_client import Counter

import sygnal.db
from sygnal.exceptions import InvalidNotificationException


NOTIFS_RECEIVED_COUNTER = Counter(
    "sygnal_notifications_received", "Number of notification pokes received",
)

NOTIFS_RECEIVED_DEVICE_PUSH_COUNTER = Counter(
    "sygnal_notifications_devices_received",
    "Number of devices been asked to push",
)

NOTIFS_BY_PUSHKIN = Counter(
    "sygnal_per_pushkin_type",
    "Number of pushes sent via each type of pushkin",
    labelnames=["pushkin"],
)


logger = logging.getLogger(__name__)

app = Flask('sygnal')
app.debug = False
app.config.from_object(__name__)

CONFIG_SECTIONS = ['http', 'log', 'apps', 'db', 'metrics']
CONFIG_DEFAULTS = {
    'port': '5000',
    'loglevel': 'info',
    'logfile': '',
    'dbfile': 'sygnal.db'
}

pushkins = {}

class RequestIdFilter(logging.Filter):
    """A logging filter which adds the current request id to each record"""
    def filter(self, record):
        request_id = ''
        if flask.has_request_context():
            request_id = flask.g.get('request_id', '')
        record.request_id = request_id
        return True

class RequestCounter(object):
    def __init__(self):
        self._count = 0
        self._lock = threading.Lock()

    def get(self):
        with self._lock:
            c = self._count
            self._count = c + 1
        return c


request_count = RequestCounter()


class Tweaks:
    def __init__(self, raw):
        self.sound = None

        if 'sound' in raw:
            self.sound = raw['sound']


class Device:
    def __init__(self, raw):
        self.app_id = None
        self.pushkey = None
        self.pushkey_ts = 0
        self.data = None
        self.tweaks = None

        if 'app_id' not in raw:
            raise InvalidNotificationException("Device with no app_id")
        if 'pushkey' not in raw:
            raise InvalidNotificationException("Device with no pushkey")
        if 'pushkey_ts' in raw:
            self.pushkey_ts = raw['pushkey_ts']
        if 'tweaks' in raw:
            self.tweaks = Tweaks(raw['tweaks'])
        else:
            self.tweaks = Tweaks({})
        self.app_id = raw['app_id']
        self.pushkey = raw['pushkey']
        if 'data' in raw:
            self.data = raw['data']


class Counts:
    def __init__(self, raw):
        self.unread = None
        self.missed_calls = None

        if 'unread' in raw:
            self.unread = raw['unread']
        if 'missed_calls' in raw:
            self.missed_calls = raw['missed_calls']


class Notification:
    def __init__(self, notif):
        optional_attrs = [
            'room_name',
            'room_alias',
            'prio',
            'membership',
            'sender_display_name',
            'content',
            'event_id',
            'room_id',
            'user_is_target',
            'type',
            'sender',
        ]
        for a in optional_attrs:
            if a in notif:
                self.__dict__[a] = notif[a]
            else:
                self.__dict__[a] = None

        if 'devices' not in notif or not isinstance(notif['devices'], list):
               raise InvalidNotificationException("Expected list in 'devices' key")

        if 'counts' in notif:
            self.counts = Counts(notif['counts'])
        else:
            self.counts = Counts({})

        self.devices = [Device(d) for d in notif['devices']]


class Pushkin(object):
    def __init__(self, name):
        self.name = name

    def setup(self):
        pass

    def getConfig(self, key):
        if not self.cfg.has_option('apps', '%s.%s' % (self.name, key)):
            return None
        return self.cfg.get('apps', '%s.%s' % (self.name, key))

    def dispatchNotification(self, n):
        pass

    def shutdown(self):
        pass


class SygnalContext:
    pass


class ClientError(Exception):
    pass


def parse_config():
    cfg = configparser.SafeConfigParser(CONFIG_DEFAULTS)
    # Make keys case-sensitive
    cfg.optionxform = str
    for sect in CONFIG_SECTIONS:
        try:
            cfg.add_section(sect)
        except configparser.DuplicateSectionError:
            pass
    # it would be nice to be able to customise this the only
    # way gunicorn lets us pass parameters to our app is by
    # adding arguments to the module which is kind of grim
    cfg.read("sygnal.conf")
    return cfg

def make_pushkin(kind, name):
    if '.' in kind:
        toimport = kind
    else:
        toimport = "sygnal.%spushkin" % kind
    toplevelmodule = __import__(toimport)
    pushkinmodule = getattr(toplevelmodule, "%spushkin" % kind)
    clarse = getattr(pushkinmodule, "%sPushkin" % kind.capitalize())
    return clarse(name)


@app.before_request
def log_request():
    flask.g.request_id = "%s-%i" % (
        request.method, request_count.get(),
    )
    logger.info("Processing request %s", request.url)


@app.after_request
def log_processed_request(response):
    logger.info(
        "Processed request %s: %i",
        request.url, response.status_code,
    )
    return response

@app.errorhandler(ClientError)
def handle_client_error(e):
    resp = flask.jsonify({ 'error': { 'msg': str(e) }  })
    resp.status_code = 400
    return resp

@app.route('/')
def root():
    return ""

@app.route('/_matrix/push/v1/notify', methods=['POST'])
def notify():
    logger.warn("Request data is %s", request.data)
    try:
        body = json.loads(request.data)
    except Exception:
        raise ClientError("Expecting json request body")

    if 'notification' not in body or not isinstance(body['notification'], dict):
        msg = "Invalid notification: expecting object in 'notification' key"
        logger.warn(msg)
        flask.abort(400, msg)

    try:
        notif = Notification(body['notification'])
    except InvalidNotificationException as e:
        logger.exception("Invalid notification")
        flask.abort(400, e.message)

    if len(notif.devices) == 0:
        msg = "No devices in notification"
        logger.warn(msg)
        flask.abort(400, msg)

    NOTIFS_RECEIVED_COUNTER.inc()

    rej = []

    for d in notif.devices:
        NOTIFS_RECEIVED_DEVICE_PUSH_COUNTER.inc()

        appid = d.app_id
        if appid not in pushkins:
            logger.warn("Got notification for unknown app ID %s", appid)
            rej.append(d.pushkey)
            continue

        pushkin = pushkins[appid]
        logger.debug(
            "Sending push to pushkin %s for app ID %s",
            pushkin.name, appid,
        )

        NOTIFS_BY_PUSHKIN.labels(pushkin.name).inc()

        try:
            rej.extend(pushkin.dispatchNotification(notif))
        except:
            logger.exception("Failed to send push")
            flask.abort(500, "Failed to send push")
    return flask.jsonify({
        "rejected": rej
    })


def setup():
    cfg = parse_config()

    logging.getLogger().setLevel(getattr(logging, cfg.get('log', 'loglevel').upper()))
    logfile = cfg.get('log', 'logfile')
    if logfile != '':
        handler = WatchedFileHandler(logfile)
        handler.addFilter(RequestIdFilter())
        formatter = logging.Formatter(
            '%(asctime)s [%(process)d] %(levelname)-5s '
            '%(request_id)s %(name)s %(message)s'
        )
        handler.setFormatter(formatter)
        logging.getLogger().addHandler(handler)
    else:
        logging.basicConfig()

    if cfg.has_option("metrics", "sentry_dsn"):
        # Only import sentry if enabled
        import sentry_sdk
        sentry_sdk.init(
            dsn=cfg.get("metrics", "sentry_dsn"),
            integrations=[sentry_sdk.integrations.flask.FlaskIntegration()],
        )

    if cfg.has_option("metrics", "prometheus_port"):
        prometheus_client.start_http_server(
            port=cfg.getint("metrics", "prometheus_port"),
            addr=cfg.get("metrics", "prometheus_addr", fallback=""),
        )

    ctx = SygnalContext()
    ctx.database = sygnal.db.Db(cfg.get('db', 'dbfile'))

    for key,val in cfg.items('apps'):
        parts = key.rsplit('.', 1)
        if len(parts) < 2:
            continue
        if parts[1] == 'type':
            try:
                pushkins[parts[0]] = make_pushkin(val, parts[0])
            except:
                logger.exception("Failed to load module for kind %s", val)
                raise

    if len(pushkins) == 0:
        logger.error("No app IDs are configured. Edit sygnal.conf to define some.")
        sys.exit(1)

    for p in pushkins:
        pushkins[p].cfg = cfg
        pushkins[p].setup(ctx)
        logger.info("Configured with app IDs: %r", pushkins.keys())

    logger.error("Setup completed")

def shutdown():
    logger.info("Starting shutdown...")
    i = 0
    for p in pushkins.values():
        logger.info("Shutting down (%d/%d)..." % (i+1, len(pushkins)))
        p.shutdown()
        i += 1
    logger.info("Shutdown complete...")


setup()
