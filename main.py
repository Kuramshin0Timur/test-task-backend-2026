import logging
from typing import Dict, Iterator, List

from sqlalchemy.orm.attributes import flag_modified

from abstract_customer import create_or_update_customer, create_or_update_lead, dict_compare_merge
from amocrm import COMPANIES, CONTACTS, Customer, Lead, amocrm_get
from models import AbstractCustomer, db

logger = logging.getLogger(__name__)


class AbstractCustomerIndex:
    def __init__(self, customers: List[AbstractCustomer]):
        self.customers = list(customers)
        self.customer_ids = {}
        self.lead_ids = {}
        self.customer_logins = {}
        self.lead_logins = {}
        self.deleted_ids = set()
        self._customer_objects = {id(customer) for customer in self.customers}

        for customer in self.customers:
            self.add_customer(customer)
            self.add_leads(customer)

    @staticmethod
    def _add(mapping, key, customer):
        if not key:
            return
        values = mapping.setdefault(key, [])
        if all(item is not customer for item in values):
            values.append(customer)

    @staticmethod
    def _remove(mapping, key, customer):
        if not key:
            return
        values = mapping.get(key)
        if not values:
            return
        values = [item for item in values if item is not customer]
        if values:
            mapping[key] = values
        else:
            mapping.pop(key, None)

    def _remember(self, customer):
        object_id = id(customer)
        if object_id not in self._customer_objects:
            self.customers.append(customer)
            self._customer_objects.add(object_id)

    def add_customer(self, customer, amo_customer=None):
        self._remember(customer)
        if amo_customer is None:
            for item in customer.amo_customers or []:
                general = item.get('general') or {}
                customer_id = general.get('id')
                if customer_id is not None:
                    self.customer_ids[customer_id] = customer
                self._add(self.customer_logins, general.get('login'), customer)
            return

        self.customer_ids[amo_customer.id] = customer
        self._add(self.customer_logins, amo_customer.get_field('Логин'), customer)

    def add_leads(self, customer, amo_lead=None):
        self._remember(customer)
        if amo_lead is None:
            for item in customer.amo_leads or []:
                general = item.get('general') or {}
                lead_id = general.get('id')
                if lead_id is not None:
                    self.lead_ids[lead_id] = customer
                self._add(self.lead_logins, general.get('login'), customer)
            return

        self.lead_ids[amo_lead.id] = customer
        self._add(self.lead_logins, amo_lead.get_field('Логин'), customer)

    def remove_customer(self, customer_id):
        customer = self.customer_ids.pop(customer_id, None)
        if customer is None:
            return

        for item in customer.amo_customers or []:
            general = item.get('general') or {}
            if general.get('id') != customer_id:
                continue
            login = general.get('login')
            if not any((entry.get('general') or {}).get('login') == login
                       for entry in customer.amo_customers or []
                       if (entry.get('general') or {}).get('id') != customer_id):
                self._remove(self.customer_logins, login, customer)
            break

    def remove_lead(self, lead_id):
        customer = self.lead_ids.pop(lead_id, None)
        if customer is None:
            return

        for item in customer.amo_leads or []:
            general = item.get('general') or {}
            if general.get('id') != lead_id:
                continue
            login = general.get('login')
            if not any((entry.get('general') or {}).get('login') == login
                       for entry in customer.amo_leads or []
                       if (entry.get('general') or {}).get('id') != lead_id):
                self._remove(self.lead_logins, login, customer)
            break

    @staticmethod
    def _unique(mapping, login):
        if not login:
            return None
        values = mapping.get(login, [])
        return values[0] if len(values) == 1 else None

    def replace(self, old, new):
        for mapping in (self.customer_ids, self.lead_ids):
            for key, value in list(mapping.items()):
                if value is old:
                    mapping[key] = new

        for mapping in (self.customer_logins, self.lead_logins):
            for key, values in list(mapping.items()):
                replaced = [new if item is old else item for item in values]
                unique = []
                for item in replaced:
                    if all(existing is not item for existing in unique):
                        unique.append(item)
                mapping[key] = unique

        if old.id is not None:
            self.deleted_ids.add(old.id)
        self.add_customer(new)
        self.add_leads(new)


def lead_action(lead: Lead, index: AbstractCustomerIndex, ids_map: Dict):
    abstract_customer = index.lead_ids.get(lead.id)
    customer_by_login = index._unique(index.customer_logins, lead.get_field('Логин'))

    if abstract_customer is None:
        abstract_customer = customer_by_login
    elif customer_by_login is not None and customer_by_login is not abstract_customer:
        if abstract_customer.id is None or customer_by_login.id is None:
            db.session.flush()

        abstract_customer.amo_customers = dict_compare_merge(
            abstract_customer.amo_customers or [], customer_by_login.amo_customers or [])
        abstract_customer.amo_leads = dict_compare_merge(
            abstract_customer.amo_leads or [], customer_by_login.amo_leads or [])
        abstract_customer.amo_contacts = dict_compare_merge(
            abstract_customer.amo_contacts or [], customer_by_login.amo_contacts or [])
        abstract_customer.amo_companies = dict_compare_merge(
            abstract_customer.amo_companies or [], customer_by_login.amo_companies or [])

        for attr in ('amo_customers', 'amo_leads', 'amo_contacts', 'amo_companies'):
            flag_modified(abstract_customer, attr)

        db.session.delete(customer_by_login)
        old_id = customer_by_login.id
        if old_id in ids_map:
            old_ids = ids_map.pop(old_id)
            target_ids = ids_map.setdefault(abstract_customer.id, {
                'customers': [], 'leads': [], 'contacts': [], 'companies': []
            })
            for key in target_ids:
                target_ids[key].extend(old_ids.get(key, []))

        index.replace(customer_by_login, abstract_customer)

    if abstract_customer is not None:
        index.remove_lead(lead.id)

    abstract_customer = create_or_update_lead(abstract_customer, lead)
    index.add_leads(abstract_customer, lead)
    return abstract_customer


def _collect_leads_customers(is_full=True):
    session = db.session
    abstract_customers = session.query(AbstractCustomer).all()
    customer_index = AbstractCustomerIndex(abstract_customers)
    ids_map = {}
    pending_customers = []
    pending_leads = []

    def get_ids(abstract_customer):
        return ids_map.setdefault(abstract_customer.id, {
            'customers': [], 'leads': [], 'contacts': [], 'companies': []
        })

    def save_customer_ids(items):
        for abstract_customer, customer in items:
            entity_ids = get_ids(abstract_customer)
            entity_ids['customers'].append(customer.id)
            entity_ids['contacts'].extend(item['id'] for item in customer.contacts)
            entity_ids['companies'].extend(item['id'] for item in customer.companies)

    def save_lead_ids(items):
        for abstract_customer, lead in items:
            entity_ids = get_ids(abstract_customer)
            entity_ids['leads'].append(lead.id)
            entity_ids['contacts'].extend(item['id'] for item in lead.contacts)
            entity_ids['companies'].extend(item['id'] for item in lead.companies)

    customers: Iterator[Customer] = amocrm_get(Customer, _with=f'{COMPANIES},{CONTACTS}')
    logger.info('Collected customers is_full=%s', is_full)

    for customer in customers:
        if not is_full and customer.status_id == 111349:
            continue

        customer_login = customer.get_field('Логин')
        if not customer_login:
            continue

        abstract_customer = customer_index.customer_ids.get(customer.id)
        if abstract_customer is None:
            abstract_customer = customer_index._unique(customer_index.lead_logins, customer_login)

        customer_index.remove_customer(customer.id)
        abstract_customer = create_or_update_customer(abstract_customer, customer)
        customer_index.add_customer(abstract_customer, customer)
        pending_customers.append((abstract_customer, customer))

        if len(pending_customers) >= 500:
            session.flush()
            if is_full:
                save_customer_ids(pending_customers)
            pending_customers.clear()

    if pending_customers:
        session.flush()
        if is_full:
            save_customer_ids(pending_customers)
        pending_customers.clear()

    del customers

    amo_leads: Iterator[Lead] = amocrm_get(Lead, _with=CONTACTS)
    logger.info('Collected leads is_full=%s', is_full)

    for amo_lead in amo_leads:
        if not is_full and amo_lead.status_id == 143:
            continue

        abstract_customer = lead_action(amo_lead, customer_index, ids_map)
        pending_leads.append((abstract_customer, amo_lead))

        if len(pending_leads) >= 500:
            session.flush()
            if is_full:
                save_lead_ids(pending_leads)
            pending_leads.clear()

    if pending_leads:
        session.flush()
        if is_full:
            save_lead_ids(pending_leads)
        pending_leads.clear()

    if not is_full:
        session.commit()
        return

    try:
        logger.info('Чистка покупателей общая')
        for abstract_customer in customer_index.customers:
            if abstract_customer.id in customer_index.deleted_ids:
                continue
            if abstract_customer.id not in ids_map:
                session.delete(abstract_customer)
                continue
            filter_abc_customer(abstract_customer, ids_map)
        session.commit()
    except Exception:
        session.rollback()
        raise


def collect_leads_customers(is_full=True):
    try:
        _collect_leads_customers(is_full=is_full)
    except Exception:
        db.session.rollback()
        logger.exception('CRM synchronization failed')
        raise


def filter_merge_abstract_customer(abc_customer: AbstractCustomer, attr: str,
                                   check_ids: List[int]):
    entities = getattr(abc_customer, attr) or []
    filtered = [item for item in entities
                if 'удалить' not in ((item.get('general') or {}).get('name') or '').lower()
                and (item.get('general') or {}).get('id') in check_ids]
    setattr(abc_customer, attr, filtered)


def filter_abc_customer(abc_customer: AbstractCustomer, ids_map: Dict):
    entity_ids = ids_map.get(abc_customer.id, {})
    for attr, key in (
        ('amo_leads', 'leads'),
        ('amo_customers', 'customers'),
        ('amo_contacts', 'contacts'),
        ('amo_companies', 'companies'),
    ):
        if getattr(abc_customer, attr):
            filter_merge_abstract_customer(abc_customer, attr, entity_ids.get(key, []))

    if not abc_customer.amo_leads and not abc_customer.amo_customers:
        db.session.delete(abc_customer)
