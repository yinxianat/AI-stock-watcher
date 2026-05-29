"""Cover the magic-link sign-in flow end-to-end."""


def test_request_link_emits_email_with_link(client):
    r = client.post("/auth/request-link", json={"email": "BOB@Example.com"})
    assert r.status_code == 204
    assert len(client.sent_emails) == 1
    msg = client.sent_emails[0]
    # Email is normalised to the input form (we lowercase in auth service
    # for lookup, but send to the caller-provided address).
    assert msg["to"].lower() == "bob@example.com"
    assert "token=" in msg["body_text"]


def test_verify_creates_user_and_returns_session(client):
    client.post("/auth/request-link", json={"email": "bob@example.com"})
    token = client.sent_emails[-1]["body_text"].split("token=")[1].split("\n")[0].strip()
    r = client.post("/auth/verify", json={"token": token})
    assert r.status_code == 200
    j = r.json()
    assert j["user"]["email"] == "bob@example.com"
    assert j["session_token"] and len(j["session_token"]) > 30


def test_verify_token_is_single_use(client):
    client.post("/auth/request-link", json={"email": "bob@example.com"})
    token = client.sent_emails[-1]["body_text"].split("token=")[1].split("\n")[0].strip()
    assert client.post("/auth/verify", json={"token": token}).status_code == 200
    # second use must fail
    r2 = client.post("/auth/verify", json={"token": token})
    assert r2.status_code == 400


def test_me_requires_bearer_token(client):
    assert client.get("/auth/me").status_code == 401
    assert client.get("/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_me_returns_signed_in_user(signed_in):
    r = signed_in["client"].get("/auth/me", headers=signed_in["auth"])
    assert r.status_code == 200
    assert r.json()["email"] == "alice@example.com"


def test_notify_email_change_flow(signed_in):
    c = signed_in["client"]
    r = c.post(
        "/auth/notify-email", json={"new_email": "alerts@example.com"}, headers=signed_in["auth"]
    )
    assert r.status_code == 204
    # Confirmation email goes to the NEW address.
    last = c.sent_emails[-1]
    assert last["to"] == "alerts@example.com"
    confirm_token = last["body_text"].split("token=")[1].split("\n")[0].strip()

    r2 = c.post("/auth/notify-email/confirm", json={"token": confirm_token})
    assert r2.status_code == 200
    assert r2.json()["notify_email"] == "alerts@example.com"
    assert r2.json()["notify_email_confirmed"] is True
