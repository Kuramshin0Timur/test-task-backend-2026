"""Проверки конечного результата синхронизации CRM."""

from types import SimpleNamespace
from unittest.mock import patch

import main
import abstract_customer


class FakeQuery:
    def __init__(self, session):
        self.session = session

    def all(self):
        return list(self.session.items)


class FakeSession:
    def __init__(self, items=None):
        self.items = list(items or [])
        self.next_id = max((item.id or 0 for item in self.items), default=0) + 1
        self.commits = 0
        self.rollbacks = 0

    def query(self, model):
        return FakeQuery(self)

    def add(self, item):
        if item.id is None:
            item.id = self.next_id
            self.next_id += 1
        if item not in self.items:
            self.items.append(item)

    def delete(self, item):
        if item in self.items:
            self.items.remove(item)

    def flush(self):
        for item in self.items:
            if item.id is None:
                item.id = self.next_id
                self.next_id += 1

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def crm_entity(entity_id, login, name, status_id=1):
    return SimpleNamespace(
        id=entity_id,
        name=name,
        status_id=status_id,
        custom_fields_values={
            'Логин': {'value': login}
        },
        contacts=[],
        companies=[],
        get_field=lambda field: login if field == 'Логин' else None,
        as_dict=lambda: {
            'id': entity_id,
            'name': name,
            'custom_fields_values': [
                {
                    'field_name': 'Логин',
                    'values': [{'value': login}]
                }
            ]
        },
    )

class FakeAbstractCustomer:
    def __init__(self, amo_customers=None, amo_leads=None, id=None):
        self.id = id
        self.amo_customers = list(amo_customers or [])
        self.amo_leads = list(amo_leads or [])
        self.amo_contacts = []
        self.amo_companies = []


def make_record(record_id, customers=None, leads=None):
    return FakeAbstractCustomer(
        id=record_id, amo_customers=customers, amo_leads=leads
    )


def general(entity_id, login, name):
    return {'general': {'id': entity_id, 'login': login, 'name': name},
            'api_value': {'id': entity_id, 'name': name}}


def run_with_crm(session, customers, leads, is_full=True):
    def crm_get(entity_type, **kwargs):
        return iter(customers if entity_type is main.Customer else leads)

    with patch.object(main.db, 'session', session), \
         patch.object(abstract_customer.db, 'session', session), \
         patch.object(main, 'amocrm_get', side_effect=crm_get), \
         patch.object(abstract_customer, 'AbstractCustomer', FakeAbstractCustomer), \
         patch.object(main, 'flag_modified', lambda *args: None), \
         patch.object(abstract_customer, 'flag_modified', lambda *args: None):
        main.collect_leads_customers(is_full=is_full)


def test_customer_and_lead_with_same_login_end_up_in_one_record():
    session = FakeSession()
    customers = [crm_entity(10, 'client-1', 'Customer')]
    leads = [crm_entity(20, 'client-1', 'Lead')]

    run_with_crm(session, customers, leads)

    assert len(session.items) == 1
    result = session.items[0]
    assert [item['general']['id'] for item in result.amo_customers] == [10]
    assert [item['general']['id'] for item in result.amo_leads] == [20]


def test_repeated_sync_does_not_create_duplicate_records():
    session = FakeSession()
    customers = [crm_entity(10, 'client-1', 'Customer')]
    leads = [crm_entity(20, 'client-1', 'Lead')]

    run_with_crm(session, customers, leads)
    run_with_crm(session, customers, leads)

    assert len(session.items) == 1
    assert len(session.items[0].amo_customers) == 1
    assert len(session.items[0].amo_leads) == 1


def test_partial_sync_does_not_delete_existing_record():
    old_record = make_record(1, customers=[general(99, 'old-client', 'Old customer')])
    session = FakeSession([old_record])

    run_with_crm(session, [], [], is_full=False)

    assert old_record in session.items
    assert session.commits == 1


def test_full_sync_removes_record_missing_from_crm():
    old_record = make_record(1, customers=[general(99, 'old-client', 'Old customer')])
    session = FakeSession([old_record])

    run_with_crm(session, [], [], is_full=True)

    assert old_record not in session.items
    assert session.commits == 1
