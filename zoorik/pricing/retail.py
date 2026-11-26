"""The Azure Retail Prices API, which both Azure price lists read: the items for a filter, every page followed."""

import urllib.parse
from dataclasses import dataclass
from typing import List, Optional

from zoorik.cloud.fields import Record
from zoorik.system.http import HttpClient

RETAIL_PRICES_URL = "https://prices.azure.com/api/retail/prices"


@dataclass(frozen=True)
class RetailItem:
    """One meter's price in the Azure Retail Prices API, in US dollars."""

    sku: str
    product: str
    meter: str
    unit: str
    price: float
    # The usage a graduated price starts at; 0 for a meter with one price.
    tier_minimum: float


def retail_items(http: HttpClient, query: str) -> List[RetailItem]:
    """Read every item the Azure Retail Prices API gives for an OData filter, following its pages."""
    url: Optional[str] = RETAIL_PRICES_URL + "?" + urllib.parse.urlencode({"$filter": query})
    items: List[RetailItem] = []
    while url is not None:
        page = Record(http.get_json(url), "the Azure Retail Prices API")
        items.extend(_item(item) for item in page.records("Items"))
        url = page.optional_text("NextPageLink")
    return items


def _item(record: Record) -> RetailItem:
    """Read one item of a page."""
    return RetailItem(
        sku=record.text("armSkuName"),
        product=record.text("productName"),
        meter=record.text("meterName"),
        unit=record.text("unitOfMeasure"),
        price=record.number("unitPrice"),
        tier_minimum=record.number("tierMinimumUnits"),
    )
