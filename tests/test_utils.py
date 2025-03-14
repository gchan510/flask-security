"""
test_utils
~~~~~~~~~~

Test utils

:copyright: (c) 2019-2025 by J. Christopher Wagner (jwag).
:license: MIT, see LICENSE for more details.
"""

from __future__ import annotations

from contextlib import contextmanager
import re
import time

from flask.json.tag import TaggedJSONSerializer
from flask.signals import message_flashed

from flask_security import (
    Security,
    SmsSenderBaseClass,
    SmsSenderFactory,
    UserMixin,
)
from flask_security.signals import (
    login_instructions_sent,
    reset_password_instructions_sent,
    tf_security_token_sent,
    user_registered,
    us_security_token_sent,
    username_recovery_email_sent,
)
from flask_security.utils import hash_data, hash_password

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.http import parse_cookie

_missing = object


def authenticate(
    client,
    email="matt@lp.com",
    password="password",
    endpoint=None,
    csrf=False,
    **kwargs,
):
    data = dict(email=email, password=password, remember="y")
    if csrf:
        response = client.get(endpoint or "/login")
        data["csrf_token"] = get_form_input_value(response, "csrf_token")
    return client.post(endpoint or "/login", data=data, **kwargs)


def json_authenticate(client, email="matt@lp.com", password="password", endpoint=None):
    data = dict(email=email, password=password)

    # Get auth token always
    ep = endpoint or "/login?include_auth_token"
    return client.post(ep, content_type="application/json", json=data)


def is_authenticated(client, get_message, auth_token=None):
    # Return True is 'client' is authenticated.
    # Return False if not
    # Raise ValueError not certain...
    headers = {"accept": "application/json"}
    if auth_token:
        headers["Authentication-Token"] = auth_token
    response = client.get("/profile", headers=headers)
    if response.status_code == 200:
        return True
    if response.status_code == 401 and response.json["response"]["errors"][0].encode(
        "utf-8"
    ) == get_message("UNAUTHENTICATED"):
        return False
    raise ValueError("Failed to figure out if authenticated")


def check_location(app, location, expected_base):
    # verify response location. Historically this can be absolute or relative based
    # on configuration. As of 5.4 and Werkzeug 2.1 it is always relative
    return location == expected_base


def verify_token(client_nc, token, status=None):
    # Use passed auth token in API that requires auth and verify status.
    # Pass in a client_nc to get valid results.
    response = client_nc.get(
        "/token",
        headers={"Content-Type": "application/json", "Authentication-Token": token},
    )
    if status:
        assert response.status_code == status
    else:
        assert b"Token Authentication" in response.data


def logout(client, endpoint=None, **kwargs):
    return client.get(endpoint or "/logout", **kwargs)


def json_logout(client, token, endpoint=None):
    return client.post(
        endpoint or "/logout",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authentication-Token": token,
        },
    )


def get_csrf_token(client):
    response = client.get(
        "/login",
        data={},
        headers={"Accept": "application/json"},
    )
    return response.json["response"]["csrf_token"]


def get_session(response):
    """Return session cookie contents.
    This a base64 encoded json.
    Returns a dict
    """

    # Alas seems like if there are multiple set-cookie headers - we are on our own
    for index, h in enumerate(response.headers):
        if h[0] == "Set-Cookie":
            cookie = parse_cookie(response.headers[index][1])
            encoded_cookie = cookie.get("session", None)
            if encoded_cookie:
                serializer = URLSafeTimedSerializer(
                    "secret", serializer=TaggedJSONSerializer()
                )
                val = serializer.loads_unsafe(encoded_cookie)
                return val[1]


def setup_tf_sms(client, url_prefix=None, csrf_token=None):
    # Simple setup of SMS as a second factor and return the sender so caller
    # can get codes.
    SmsSenderFactory.senders["test"] = SmsTestSender
    sms_sender = SmsSenderFactory.createSender("test")
    data = dict(setup="sms", phone="+442083661188", csrf_token=csrf_token)
    response = client.post("/".join(filter(None, (url_prefix, "tf-setup"))), json=data)
    assert sms_sender.get_count() == 1
    code = sms_sender.messages[0].split()[-1]
    response = client.post(
        "/".join(filter(None, (url_prefix, "tf-validate"))),
        json=dict(code=code, csrf_token=csrf_token),
    )
    assert response.status_code == 200
    return sms_sender


def get_existing_session(client):
    cookie = client.get_cookie("session")
    if cookie:
        serializer = URLSafeTimedSerializer("secret", serializer=TaggedJSONSerializer())
        val = serializer.loads_unsafe(cookie.value)
        return val[1]


def reset_fresh(client, within):
    # Assumes client authenticated.
    # Upon return the NEXT request if protected with a freshness check
    # will require a fresh authentication.
    with client.session_transaction() as sess:
        old_paa = sess["fs_paa"] - within.total_seconds() - 100
        sess["fs_paa"] = old_paa
        sess.pop("fs_gexp", None)
    return old_paa


def reset_fresh_auth_token(app, within, email="matt@lp.com"):
    # Assumes client authenticated.
    # Returns a new auth token that will force the NEXT request,
    # if protected with a freshness check to require a fresh authentication
    with app.test_request_context("/"):
        user = app.security.datastore.find_user(email=email)
        tdata = dict(ver=str(5))
        if hasattr(user, "fs_token_uniquifier"):
            tdata["uid"] = str(user.fs_token_uniquifier)
        else:
            tdata["uid"] = str(user.fs_uniquifier)
        tdata["fs_paa"] = time.time() - within.total_seconds() - 100
        tdata["exp"] = int(app.config.get("SECURITY_TOKEN_EXPIRE_TIMESTAMP")(user))
        return app.security.remember_token_serializer.dumps(tdata)


def get_form_action(response, ordinal=0):
    # Return the URL that the form WOULD post to - this is useful to check
    # how our templates actually work (e.g. propagation of 'next')
    matcher = re.findall(
        r'(?:<form action|formaction)="(\S*)"',
        response.data.decode("utf-8"),
        re.IGNORECASE | re.DOTALL,
    )
    return matcher[ordinal]


def _parse_form_input(response, rex):
    matcher = re.findall(
        rex,
        response.data.decode("utf-8"),
        re.IGNORECASE | re.DOTALL,
    )
    if matcher:
        return matcher[0]
    return None


def get_form_input(response, field_id):
    # return entire input field for field with the id == field_id or None if not found
    return _parse_form_input(response, f'<input ([^>]*id="{field_id}"[^>]*)">')


def get_form_input_value(response, field_id):
    # return 'value' of field with the id == field_id or None if not found
    return _parse_form_input(
        response, f'<input [^>]*id="{field_id}"[^>]*value="([^"]*)">'
    )


def check_xlation(app, locale):
    """Return True if locale is loaded"""
    with app.test_request_context():
        domain = app.security.i18n_domain
        xlations = domain.get_translations()
        if not xlations:
            return False
        # Flask-Babel doesn't populate _info as Flask-BabelEx did - so look in first
        # string which is catalog info.
        matcher = re.search(r"Language:\s*(\w+)", xlations._catalog[""])
        return matcher.group(1) == locale


def create_roles(ds):
    roles = [
        ("admin", ["full-read", "full-write", "super"]),
        ("editor", ["full-read", "full-write"]),
        ("author", ["full-read", "my-write"]),
        ("simple", None),
    ]
    for role in roles:
        if hasattr(ds.role_model, "permissions") and role[1]:
            ds.create_role(name=role[0], permissions=role[1])
        else:
            ds.create_role(name=role[0])
    ds.commit()


def create_users(app, ds, count=None):
    users = [
        ("matt@lp.com", "matt", "password", ["admin"], True, 123456, None),
        ("joe@lp.com", "joe", "password", ["editor"], True, 234567, None),
        ("dave@lp.com", "dave", "password", ["admin", "editor"], True, 345678, None),
        ("jill@lp.com", "jill", "password", ["author"], True, 456789, None),
        ("tiya@lp.com", "tiya", "password", [], False, 567890, None),
        ("gene@lp.com", "gene", "password", ["simple"], True, 889900, None),
        ("jess@lp.com", "jess", None, [], True, 678901, None),
        ("gal@lp.com", "gal", "password", ["admin"], True, 112233, "sms"),
        ("gal2@lp.com", "gal2", "password", ["admin"], True, 223311, "authenticator"),
        ("gal3@lp.com", "gal3", "password", ["admin"], True, 331122, "email"),
    ]
    count = count or len(users)

    for u in users[:count]:
        pw = u[2]
        if pw is not None:
            pw = hash_password(pw)
        roles = [ds.find_or_create_role(rn) for rn in u[3]]
        ds.commit()
        totp_secret = None
        if app.config.get("SECURITY_TWO_FACTOR", None) and u[6]:
            totp_secret = app.security._totp_factory.generate_totp_secret()
        user = ds.create_user(
            email=u[0],
            username=u[1],
            password=pw,
            active=u[4],
            security_number=u[5],
            tf_primary_method=u[6],
            tf_totp_secret=totp_secret,
        )
        ds.commit()
        for role in roles:
            ds.add_role_to_user(user, role)
        ds.commit()


def populate_data(app, user_count=None):
    ds = app.security.datastore
    with app.app_context():
        create_roles(ds)
        create_users(app, ds, user_count)


def init_app_with_options(app, datastore, **options):
    security_args = options.pop("security_args", {})
    app.config.update(**options)
    app.security = Security(app, datastore=datastore, **security_args)
    populate_data(app)


@contextmanager
def capture_queries(datastore):
    from sqlalchemy import event

    queries = []

    @event.listens_for(datastore.db.session, "do_orm_execute")
    def _do_orm_execute(orm_execute_state):
        queries.append(orm_execute_state)

    yield queries


class SmsTestSender(SmsSenderBaseClass):
    messages: list[str] = []
    count = 0

    # This looks strange because we need class variables since test need to access a
    # sender but the actual sender is instantiated low down in SMS code.
    def __init__(self):
        super().__init__()
        SmsTestSender.count = 0
        SmsTestSender.messages = []

    def send_sms(self, from_number, to_number, msg):
        SmsTestSender.messages.append(msg)
        SmsTestSender.count += 1
        return

    def get_count(self):
        return SmsTestSender.count


class SmsBadSender(SmsSenderBaseClass):
    def send_sms(self, from_number, to_number, msg):
        raise ValueError(f"Unknown number: {to_number}")


@contextmanager
def capture_passwordless_login_requests():
    login_requests = []

    def _on(app, **data):
        login_requests.append(data)

    login_instructions_sent.connect(_on)

    try:
        yield login_requests
    finally:
        login_instructions_sent.disconnect(_on)


@contextmanager
def capture_registrations():
    """Testing utility for capturing registrations."""
    registrations = []

    def _on(app, **data):
        data["email"] = data["user"].email
        registrations.append(data)

    user_registered.connect(_on)

    try:
        yield registrations
    finally:
        user_registered.disconnect(_on)


@contextmanager
def capture_reset_password_requests(reset_password_sent_at=None):
    """Testing utility for capturing password reset requests.

    :param reset_password_sent_at: An optional datetime object to set the
                                   user's `reset_password_sent_at` to
    """
    reset_requests = []

    def _on(app, **data):
        reset_requests.append(data)

    reset_password_instructions_sent.connect(_on)

    try:
        yield reset_requests
    finally:
        reset_password_instructions_sent.disconnect(_on)


@contextmanager
def capture_username_recovery_requests():
    """Testing utility for capturing username recovery requests."""
    recovery_requests = []

    def _on(app, **data):
        recovery_requests.append(data)

    username_recovery_email_sent.connect(_on)

    try:
        yield recovery_requests
    finally:
        username_recovery_email_sent.disconnect(_on)


@contextmanager
def capture_flashes():
    """Testing utility for capturing flashes."""
    flashes = []

    def _on(app, **data):
        flashes.append(data)

    message_flashed.connect(_on)

    try:
        yield flashes
    finally:
        message_flashed.disconnect(_on)


@contextmanager
def capture_send_code_requests():
    # Easy way to get token/code required for code logins
    # either second factor or us_signin
    login_requests = []

    def _on(app, **data):
        assert all(v in data for v in ["user", "method", "login_token"])
        assert isinstance(data["user"], UserMixin)
        login_requests.append(data)

    us_security_token_sent.connect(_on)
    tf_security_token_sent.connect(_on)

    try:
        yield login_requests
    finally:
        us_security_token_sent.disconnect(_on)
        tf_security_token_sent.disconnect(_on)


def get_auth_token_version_3x(app, user):
    """
    Copy of algorithm that generated user token in version 3.x
    """
    data = [str(user.id), hash_data(user.password)]
    if hasattr(user, "fs_uniquifier"):
        data.append(user.fs_uniquifier)
    return app.security.remember_token_serializer.dumps(data)


def get_auth_token_version_4x(app, user):
    """Copy of algorithm that generated user token in version 4.x- 5.4"""
    data = [str(user.fs_uniquifier)]
    return app.security.remember_token_serializer.dumps(data)


class FakeSerializer:
    def __init__(self, age=None, invalid=False):
        self.age = age
        self.invalid = invalid

    def loads(self, token, max_age):
        if self.age:
            assert max_age == self.age
            raise SignatureExpired("expired")
        if self.invalid:
            raise BadSignature("bad")

    def loads_unsafe(self, token):
        return None, None

    def dumps(self, state):
        return "heres your state"


def convert_bool_option(v):
    # Used for command line options to convert string to bool
    if str(v).lower() in ["true"]:
        return True
    elif str(v).lower() in ["false"]:
        return False
    return v
