import json
import logging
from collections import defaultdict
from typing import Optional, Dict

from sqlalchemy import text
from sqlalchemy.orm.attributes import flag_modified

from amocrm import Customer, Lead
from models import AbstractCustomer, db, AbcCustomerEntities

logger = logging.getLogger(__name__)


def update_jsonb_field(entity_id: int,
                       attribute: AbcCustomerEntities,
                       index: int, new_value: Dict,
                       table_name='abstract_customer'):
    """Обновление jsonb поля в указанной таблице.
    :param table_name: Название таблицы (должно быть прописано).
    :param entity_id: ид абстрактного покупателя.
    :param attribute: Поле таблицы, которое надо обновить в формате Enum.
    :param index: Индекс изменения - цифра если массив, массив строк если словарь. Цифра должна быть точной.
    :param new_value: Новые данные для изменения
    """
    if table_name != 'abstract_customer':
        raise Exception("Неизвестная таблица")
    query = text(f"""
        UPDATE {table_name}
        SET {attribute.value} = jsonb_set({attribute.value}, :path, :value)
        WHERE id = :entity_id
    """)
    db.session.execute(query, {'path': f'{{{index}}}', 'value': json.dumps(new_value), 'entity_id': entity_id})


def dict_compare_merge(l_dict1, l_dict2):
    d = defaultdict(dict)
    for i in l_dict1 + l_dict2:
        d[i['general']['id']].update(i)

    return list(d.values())


def get_customer_json(customer: Customer) -> dict:
    return {
        'general': {
            'id': customer.id,
            'login': customer.custom_fields_values.get('Логин', {}).get('value'),
            'billing_type': customer.custom_fields_values.get('Вид оплаты', {}).get('value'),
            'is_robot': bool(customer.custom_fields_values.get('Робот', {}).get('value')),
            'is_nds': bool(customer.custom_fields_values.get('Оплата с НДС', {}).get('value')),
            'system': customer.custom_fields_values.get('Система', {}).get('value'),
            'host': customer.custom_fields_values.get('Хост', {}).get('value'),
            'name': customer.name,
        },
        'api_value': customer.as_dict()
    }


def get_lead_json(lead: Lead) -> dict:
    return {
        'general': {
            'id': lead.id,
            'login': lead.custom_fields_values.get('Логин', {}).get('value'),
            'name': lead.name,
        },
        'api_value': lead.as_dict()
    }


def get_abccustomer_by_lead_login(lead_login: str) -> Optional[AbstractCustomer]:
    customers = db.session.query(AbstractCustomer).filter(
        AbstractCustomer.amo_leads.op('@>')([{'general': {'login': lead_login}}])).all()

    if len(customers) > 1:
        logger.warning(f"Lead {lead_login} has more than one abccustomer")
        return None
    if not customers:
        return None

    return customers[0]


def get_abccustomer_by_amocustomer_login(customer_login: str) -> Optional[AbstractCustomer]:
    customers = db.session.query(AbstractCustomer).filter(
        AbstractCustomer.amo_customers.op('@>')([{'general': {'login': customer_login}}])).all()

    if len(customers) > 1:
        logger.warning(f"Customer {customer_login} has more than one abccustomer")
        return None
    if not customers:
        return None

    return customers[0]


def create_or_update_customer(abstract_customer: Optional[AbstractCustomer], amo_customer: Customer):
    if abstract_customer is None:
        new_abstract_customer = AbstractCustomer(
            amo_customers=[get_customer_json(amo_customer)],
        )
        db.session.add(new_abstract_customer)
        return new_abstract_customer

    abc_amo_customers = abstract_customer.amo_customers
    if not abc_amo_customers:
        # Не было покупателей
        abstract_customer.amo_customers = [get_customer_json(amo_customer)]
        return abstract_customer

    customer_data = get_customer_json(amo_customer)
    index = next((idx for idx, item in enumerate(abc_amo_customers)
                  if item['general']['id'] == amo_customer.id), None)
    if index is None:
        abc_amo_customers.append(customer_data)
    else:
        abc_amo_customers[index] = customer_data
    abstract_customer.amo_customers = abc_amo_customers
    flag_modified(abstract_customer, 'amo_customers')
    return abstract_customer


def create_or_update_lead(abc_customer: Optional[AbstractCustomer], amo_lead: Lead) -> AbstractCustomer:
    if abc_customer is None:
        new_abstract_customer = AbstractCustomer(
            amo_leads=[get_lead_json(amo_lead)])
        db.session.add(new_abstract_customer)
        return new_abstract_customer

    if not abc_customer.amo_leads:
        abc_customer.amo_leads = [get_lead_json(amo_lead)]
        return abc_customer

    lead_data = get_lead_json(amo_lead)
    index = next((idx for idx, item in enumerate(abc_customer.amo_leads)
                  if item['general']['id'] == amo_lead.id), None)
    if index is None:
        abc_customer.amo_leads.append(lead_data)
    else:
        abc_customer.amo_leads[index] = lead_data
    flag_modified(abc_customer, 'amo_leads')
    return abc_customer
