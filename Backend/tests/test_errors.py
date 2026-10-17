from fastapi import APIRouter

from app.core.exceptions import NotFoundError


def test_app_error_shape(client):
    router = APIRouter()

    @router.get("/_boom_nf")
    def nf():
        raise NotFoundError("thing missing")

    @router.get("/_boom_500")
    def boom():
        raise RuntimeError("secret internals")

    client.app.include_router(router)
    r = client.get("/_boom_nf")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
    assert r.json()["error"]["request_id"]

    r = client.get("/_boom_500")
    assert r.status_code == 500
    assert "secret internals" not in r.text  # no internal leakage