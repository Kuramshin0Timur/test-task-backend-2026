import logging
from typing import Iterator, List, Dict

from sqlalchemy.orm.attributes import flag_modified

from abstract_customer import create_or_update_customer, dict_compare_merge, create_or_update_lead
from amocrm import Customer, COMPANIES, CONTACTS, Lead, amocrm_get
from models import db, AbstractCustomer

logger = logging.getLogger(__name__)


class AbstractCustomerIndex:
    def __init__(self, customers):
        self.customers = customers
        self.customer_ids = {}
        self.lead_ids = {}
        self.customer_logins = {}
        self.lead_logins = {}
        self.deleted_ids = set()
        self.customer_object_ids = {customer.id for customer in customers}
        for customer in customers:
            self.add_customer(customer)
            self.add_leads(customer)

    @staticmethod
    def _add(mapping, key, customer):
        if key is None:
            return
        values = mapping.setdefault(key, [])
        if all(item.id != customer.id for item in values):
            values.append(customer)

    def _remember(self, customer):
        if customer.id not in self.customer_object_ids:
            self.customers.append(customer)
            self.customer_object_ids.add(customer.id)

    def add_customer(self, customer, amo_customer=None):
        self._remember(customer)
        if amo_customer is None:
            for item in customer.amo_customers or []:
                self.customer_ids[item['general']['id']] = customer
                self._add(self.customer_logins, item['general'].get('login'), customer)
        else:
            self.customer_ids[amo_customer.id] = customer
            self._add(self.customer_logins, amo_customer.get_field('Логин'), customer)

    def add_leads(self, customer, amo_lead=None):
        self._remember(customer)
        if amo_lead is None:
            for item in customer.amo_leads or []:
                self.lead_ids[item['general']['id']] = customer
                self._add(self.lead_logins, item['general'].get('login'), customer)
        else:
            self.lead_ids[amo_lead.id] = customer
            self._add(self.lead_logins, amo_lead.get_field('Логин'), customer)

    @staticmethod
    def _unique(mapping, login):
        values = mapping.get(login, [])
        return values[0] if len(values) == 1 else None

    def replace(self, old, new):
        for mapping in (self.customer_ids, self.lead_ids):
            for key, value in list(mapping.items()):
                if value.id == old.id:
                    mapping[key] = new
        for mapping in (self.customer_logins, self.lead_logins):
            for key, values in list(mapping.items()):
                mapping[key] = [new if item.id == old.id else item for item in values]
                mapping[key] = list({item.id: item for item in mapping[key]}.values())
        self.deleted_ids.add(old.id)
        self.add_customer(new)
        self.add_leads(new)


def lead_action(lead: Lead, index: AbstractCustomerIndex, ids_map):
    abstract_customer = index.lead_ids.get(lead.id)
    lead_login = lead.get_field('Логин')
    if lead_login:
        abs_customer_login = index._unique(index.customer_logins, lead_login)
        if abstract_customer is None:
            abstract_customer = abs_customer_login
        elif abs_customer_login is not None and abs_customer_login.id != abstract_customer.id:
            abstract_customer.amo_customers = dict_compare_merge(abstract_customer.amo_customers or [],
                                                                 abs_customer_login.amo_customers or [])
            abstract_customer.amo_leads = dict_compare_merge(abstract_customer.amo_leads or [],
                                                             abs_customer_login.amo_leads or [])
            abstract_customer.amo_contacts = dict_compare_merge(abstract_customer.amo_contacts or [],
                                                                abs_customer_login.amo_contacts or [])
            abstract_customer.amo_companies = dict_compare_merge(abstract_customer.amo_companies or [],
                                                                 abs_customer_login.amo_companies or [])
            for attr in ('amo_customers', 'amo_leads', 'amo_contacts', 'amo_companies'):
                flag_modified(abstract_customer, attr)
            db.session.delete(abs_customer_login)
            if abs_customer_login.id in ids_map:
                merged_ids = ids_map.pop(abs_customer_login.id)
                target_ids = ids_map.setdefault(abstract_customer.id, {
                    "customers": [], "leads": [], "contacts": [], "companies": []
                })
                for key in target_ids:
                    target_ids[key].extend(merged_ids.get(key, []))
            index.replace(abs_customer_login, abstract_customer)
            abstract_customer = create_or_update_lead(abstract_customer, lead)
            index.add_leads(abstract_customer, lead)
            return abstract_customer

    abstract_customer = create_or_update_lead(abstract_customer, lead)
    index.add_leads(abstract_customer, lead)
    return abstract_customer


def collect_leads_customers(is_full=True):
    """
     Берем всех покупателей, если его нет в бд - делаем новую запись AbstractCutomer, в него кладем.
     Есть есть - находим его запись по id и перезаписываем

     Вот так у нас коллектятся все кастомеры

     Далее берутся все лиды. Ищутся их записи в бд по id, если не находится и есть логин
     - находим его запись по кастомеру с тем же логином, кладем к нему.
     Если нет логина/нет кастомера - пропускаем. Если нашелся - перезаписываем соответственно
         abstract_id_customer : {
        customers: [...],
        leads: [...],
        contacts: [...],
        companies: [...]
    }
    """
    session = db.session
    customers: Iterator[Customer] = amocrm_get(Customer, _with=f"{COMPANIES},{CONTACTS}")
    logger.info(f"Collected customers {is_full=}")

    abstract_customers = session.query(AbstractCustomer).all()
    customer_index = AbstractCustomerIndex(abstract_customers)
    ids_map = {}
    for customer in customers:
        if is_full is False and customer.status_id == 111349:
            continue
        customer_login = customer.get_field('Логин')
        if not customer_login:
            continue
        abstract_customer = customer_index.customer_ids.get(customer.id)
        if abstract_customer is None:
            abstract_customer = customer_index._unique(customer_index.lead_logins, customer_login)
        abstract_customer = create_or_update_customer(abstract_customer, customer)
        if abstract_customer.id is None:
            session.flush()
        customer_index.add_customer(abstract_customer, customer)
        if is_full is True:
            if abstract_customer.id in ids_map:
                ids_map[abstract_customer.id]["customers"].append(customer.id)
                ids_map[abstract_customer.id]["contacts"].extend([c["id"] for c in customer.contacts])
                ids_map[abstract_customer.id]["companies"].extend([c["id"] for c in customer.companies])
            else:
                ids_map[abstract_customer.id] = {
                    "customers": [customer.id],
                    "contacts": [c["id"] for c in customer.contacts],
                    "companies": [c["id"] for c in customer.companies],
                    "leads": [],
                }

    del customers
    amo_leads: Iterator[Lead] = amocrm_get(Lead, _with=f"{CONTACTS}")
    logger.info(f"Collected leads {is_full=}")
    for amo_lead in amo_leads:
        if is_full is False and amo_lead.status_id == 143:
            continue
        abc_customer = lead_action(amo_lead, customer_index, ids_map)
        if abc_customer.id is None:
            session.flush()
        if is_full is True:
            if abc_customer.id in ids_map:
                ids_map[abc_customer.id]["leads"].append(amo_lead.id)
                ids_map[abc_customer.id]["contacts"].extend([c["id"] for c in amo_lead.contacts])
            else:
                ids_map[abc_customer.id] = {
                    "customers": [],
                    "contacts": [c["id"] for c in amo_lead.contacts],
                    "companies": [],
                    "leads": [amo_lead.id],
                }

    if is_full is False:
        session.commit()
        return
    try:
        logger.info("Чистка покупателей общая")
        logger.info("Чистка покупателей на уровне сущностей")
        for abc_customer in abstract_customers:
            if abc_customer.id in customer_index.deleted_ids:
                continue
            if abc_customer.id not in ids_map:
                session.delete(abc_customer)
                continue
            filter_abc_customer(abc_customer=abc_customer, ids_map=ids_map)
        session.commit()
    except Exception:
        session.rollback()
        raise


def filter_merge_abstract_customer(abc_customer: AbstractCustomer, attr: str,
                                   check_ids: List[int]):
    entity = abc_customer.__getattribute__(attr)
    entity = [item for item in entity if
              'удалить' not in (item['general'].get('name') or '').lower()
              and item['general'].get('id') in check_ids]
    setattr(abc_customer, attr, entity)
    flag_modified(abc_customer, attr)


def filter_abc_customer(abc_customer: AbstractCustomer, ids_map: Dict):
    if abc_customer.amo_leads:
        filter_merge_abstract_customer(abc_customer=abc_customer,
                                       attr="amo_leads",
                                       check_ids=ids_map.get(abc_customer.id, {}).get("leads", []))
    if abc_customer.amo_customers:
        filter_merge_abstract_customer(abc_customer=abc_customer,
                                       attr="amo_customers",
                                       check_ids=ids_map.get(abc_customer.id, {}).get("customers", []))
    if abc_customer.amo_contacts:
        filter_merge_abstract_customer(abc_customer=abc_customer,
                                       attr="amo_contacts",
                                       check_ids=ids_map.get(abc_customer.id, {}).get("contacts", []))
    if abc_customer.amo_companies:
        filter_merge_abstract_customer(abc_customer=abc_customer,
                                       attr="amo_companies",
                                       check_ids=ids_map.get(abc_customer.id, {}).get("companies", []))
    if not abc_customer.amo_leads and not abc_customer.amo_customers:
        db.session.delete(abc_customer)
