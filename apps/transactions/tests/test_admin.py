import pytest

from apps.transactions.models import Transaction

pytestmark = pytest.mark.django_db


def test_admin_lists_transactions_read_only(admin_client, alice, food, make_transaction):
    txn = make_transaction(user=alice, category=food)

    assert admin_client.get("/admin/transactions/transaction/").status_code == 200
    assert admin_client.get("/admin/transactions/transaction/?status=deleted").status_code == 200
    assert admin_client.get(f"/admin/transactions/transaction/{txn.pk}/change/").status_code == 200
    assert admin_client.get("/admin/transactions/transaction/add/").status_code == 403

    response = admin_client.post(
        f"/admin/transactions/transaction/{txn.pk}/change/", {"amount": "999.00"}
    )
    assert response.status_code == 403
    delete_url = f"/admin/transactions/transaction/{txn.pk}/delete/"
    assert admin_client.post(delete_url, {"post": "yes"}).status_code == 403
    txn.refresh_from_db()
    assert txn.amount == 10
    assert Transaction.objects.filter(pk=txn.pk).exists()


def test_admin_index_and_changelists_render(admin_client):
    for url in (
        "/admin/",
        "/admin/accounts/user/",
        "/admin/families/family/",
        "/admin/families/familymember/",
        "/admin/categories/category/",
        "/admin/categories/subcategory/",
        "/admin/accounts/user/add/",
        "/admin/families/family/add/",
        "/admin/categories/category/add/",
    ):
        assert admin_client.get(url).status_code == 200, url
