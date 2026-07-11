"""
tests/test_notifications.py — Mixtape

Regression tests for notification creation.

test_rating_creates_notification_for_sharer is the regression test for Issue #4
("I got notified when a friend added my song to a playlist but not when they
rated it"). Before the fix, rate_song saved the Rating but never called
create_notification, so this test failed with 0 notifications. It also documents
the intended parity with the working add_to_playlist notification path.
"""

import pytest
from app import create_app, db
from models import User, Song
from services.notification_service import rate_song, get_notifications


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def sharer_and_song(app):
    """A user who shared a song, plus a second user who will interact with it."""
    with app.app_context():
        sharer = User(username="sharer", email="sharer@example.com")
        friend = User(username="friend", email="friend@example.com")
        db.session.add_all([sharer, friend])
        db.session.flush()

        song = Song(title="Shared Track", artist="Sharer", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()
        yield {"sharer": sharer, "friend": friend, "song": song}


def test_rating_creates_notification_for_sharer(app, sharer_and_song):
    """
    Regression test for Issue #4: rating a song must notify the song's sharer,
    just like adding it to a playlist does.
    """
    with app.app_context():
        sharer = sharer_and_song["sharer"]
        friend = sharer_and_song["friend"]
        song = sharer_and_song["song"]

        assert len(get_notifications(sharer.id)) == 0

        rate_song(friend.id, song.id, 5)

        notifs = get_notifications(sharer.id)
        assert len(notifs) == 1  # Bug caused this to be 0
        assert notifs[0]["type"] == "song_rated"
        assert "friend" in notifs[0]["body"]


def test_rating_own_song_does_not_notify(app, sharer_and_song):
    """A user rating their own shared song should not notify themselves."""
    with app.app_context():
        sharer = sharer_and_song["sharer"]
        song = sharer_and_song["song"]

        rate_song(sharer.id, song.id, 4)

        assert len(get_notifications(sharer.id)) == 0
