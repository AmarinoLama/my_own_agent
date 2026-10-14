"""Optimizador local de compras Cardmarket.

Arranque: python cardmarket.py
La web solo escucha en 127.0.0.1 y no solicita credenciales de Cardmarket.
"""
from __future__ import annotations

import argparse
import json
import threading
import unicodedata
import webbrowser
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from xml.sax.saxutils import escape
import zipfile

ROOT = Path(__file__).resolve().parent
HTML_FILE = ROOT / "cardmarket_optimizador.html"
MAX_UNITS = 200
MAX_SEARCH_NODES = 300_000
IMPORT_QUEUE_LOCK = threading.Lock()


def _is_same_local_origin(origin: str, host: str) -> bool:
    parsed = urlparse(origin)
    return (
        parsed.scheme == "http"
        and parsed.hostname in ("127.0.0.1", "localhost")
        and parsed.netloc == host
    )


def _is_same_local_referrer(referrer: str, host: str) -> bool:
    parsed = urlparse(referrer)
    return (
        parsed.scheme == "http"
        and parsed.hostname in ("127.0.0.1", "localhost")
        and parsed.netloc == host
    )


def _key(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.strip().casefold())
    return " ".join("".join(ch for ch in value if not unicodedata.combining(ch)).split())


def _money_cents(value: object, field: str) -> int:
    try:
        amount = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        raise ValueError(f"El campo «{field}» debe ser un precio numérico.") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"El campo «{field}» debe ser un precio válido y no negativo.")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _quantity(value: object, field: str, maximum: int = 200) -> int:
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"El campo «{field}» debe ser un número entero.") from None
    if not parsed.is_finite() or parsed != parsed.to_integral_value() or not 1 <= parsed <= maximum:
        raise ValueError(f"El campo «{field}» debe ser un entero entre 1 y {maximum}.")
    return int(parsed)


def _money(cents: int) -> float:
    return round(cents / 100, 2)


def optimize(wants: list[dict], offers: list[dict], node_limit: int = MAX_SEARCH_NODES) -> dict:
    """Maximiza copias cubiertas y, a igual cobertura, maximiza trend menos coste total.

    Cada copia solicitada se puede asignar a un vendedor distinto. Cada oferta tiene
    stock propio y el envío se cobra una vez por vendedor activo. La búsqueda usa
    branch-and-bound y comunica si alcanzó el límite antes de probar optimalidad.
    """
    if not isinstance(wants, list) or not isinstance(offers, list):
        raise ValueError("Las listas de wants y ofertas deben ser listas de filas.")
    if any(not isinstance(row, dict) for row in wants):
        raise ValueError("Cada want debe ser una fila con persona, carta, cantidad y trend.")
    if any(not isinstance(row, dict) for row in offers):
        raise ValueError("Cada oferta debe ser una fila con vendedor, carta, precio, stock y envío.")
    if not isinstance(node_limit, int) or node_limit < 1:
        raise ValueError("El límite de búsqueda debe ser un entero positivo.")
    if len(wants) > 2_000 or len(offers) > 5_000:
        raise ValueError("El archivo es demasiado grande para procesarlo (máximo 2.000 wants y 5.000 ofertas).")

    demand_rows = []
    total_units = 0
    for line_no, row in enumerate(wants, 1):
        person = str(row.get("persona", "")).strip()
        card = str(row.get("carta", "")).strip()
        if not person or not card:
            raise ValueError(f"Want {line_no}: indica persona y carta.")
        person_key, card_key = _key(person), _key(card)
        if not person_key or not card_key:
            raise ValueError(f"Want {line_no}: persona o carta no válidas.")
        quantity = _quantity(row.get("cantidad", ""), f"cantidad del want {line_no}")
        trend = _money_cents(row.get("trend", row.get("tendencia", "")), f"trend del want {line_no}")
        total_units += quantity
        if total_units > MAX_UNITS:
            raise ValueError(f"El optimizador admite hasta {MAX_UNITS} copias solicitadas por cálculo.")
        demand_rows.append({
            "person": person, "person_key": person_key, "card": card,
            "card_key": card_key, "quantity": quantity, "trend": trend,
        })
    if not demand_rows:
        raise ValueError("Añade al menos una fila a la lista de wants.")

    parsed_offers = []
    seller_names: dict[str, str] = {}
    seller_shipping: dict[str, int] = {}
    card_offers: dict[str, list[int]] = defaultdict(list)
    for line_no, row in enumerate(offers, 1):
        seller = str(row.get("vendedor", "")).strip()
        card = str(row.get("carta", "")).strip()
        if not seller or not card:
            raise ValueError(f"Oferta {line_no}: indica vendedor y carta.")
        seller_key, card_key = _key(seller), _key(card)
        if not seller_key or not card_key:
            raise ValueError(f"Oferta {line_no}: vendedor o carta no válidos.")
        price = _money_cents(row.get("precio", ""), f"precio de la oferta {line_no}")
        stock = _quantity(row.get("cantidad", ""), f"stock de la oferta {line_no}")
        shipping = _money_cents(row.get("envio", row.get("envío", "")), f"envío de la oferta {line_no}")
        if seller_key in seller_shipping and seller_shipping[seller_key] != shipping:
            raise ValueError(
                f"El vendedor «{seller_names[seller_key]}» tiene varios importes de envío. "
                "Repite el mismo envío en todas sus ofertas."
            )
        seller_names.setdefault(seller_key, seller)
        seller_shipping[seller_key] = shipping
        offer_id = len(parsed_offers)
        parsed_offers.append({
            "seller": seller_names[seller_key], "seller_key": seller_key,
            "card": card, "card_key": card_key, "price": price,
            "stock": stock, "remaining": stock, "shipping": shipping,
        })
        card_offers[card_key].append(offer_id)

    if not parsed_offers:
        raise ValueError("Añade al menos una fila de ofertas de vendedores.")

    units = []
    for demand_id, row in enumerate(demand_rows):
        options = card_offers.get(row["card_key"], [])
        for copy_no in range(row["quantity"]):
            units.append({"demand_id": demand_id, "copy_no": copy_no, "options": options})
    units.sort(key=lambda unit: (len(unit["options"]), demand_rows[unit["demand_id"]]["card_key"],
                                 demand_rows[unit["demand_id"]]["person_key"]))

    # La solución inicial greedy da una cota válida y normalmente cubre todo el stock disponible.
    remaining = [offer["stock"] for offer in parsed_offers]
    greedy_active: set[str] = set()
    greedy_assignment: list[int | None] = [None] * len(units)
    greedy_score = 0
    greedy_covered = 0
    for unit_index, unit in enumerate(units):
        available = [i for i in unit["options"] if remaining[i] > 0]
        if not available:
            continue
        demand = demand_rows[unit["demand_id"]]
        offer_id = min(available, key=lambda i: (
            parsed_offers[i]["price"] - demand["trend"]
            + (0 if parsed_offers[i]["seller_key"] in greedy_active else parsed_offers[i]["shipping"]),
            parsed_offers[i]["price"], parsed_offers[i]["seller_key"],
        ))
        offer = parsed_offers[offer_id]
        remaining[offer_id] -= 1
        if offer["seller_key"] not in greedy_active:
            greedy_score += offer["shipping"]
            greedy_active.add(offer["seller_key"])
        greedy_score += offer["price"] - demand["trend"]
        greedy_assignment[unit_index] = offer_id
        greedy_covered += 1

    best_covered, best_score, best_assignment = greedy_covered, greedy_score, greedy_assignment[:]
    remaining = [offer["stock"] for offer in parsed_offers]
    active: set[str] = set()
    assignment: list[int | None] = [None] * len(units)
    nodes = 0
    limit_reached = False

    # Cota exacta de cobertura por carta: las ofertas solo compiten por copias de esa carta.
    suffix_counts: list[dict[str, int]] = [dict() for _ in range(len(units) + 1)]
    for index in range(len(units) - 1, -1, -1):
        counts = suffix_counts[index + 1].copy()
        card_key = demand_rows[units[index]["demand_id"]]["card_key"]
        counts[card_key] = counts.get(card_key, 0) + 1
        suffix_counts[index] = counts

    def visit(index: int, covered: int, goods_cost: int, shipping_cost: int, score: int) -> None:
        nonlocal nodes, limit_reached, best_covered, best_score, best_assignment
        if nodes >= node_limit:
            limit_reached = True
            return
        nodes += 1
        # Cualquier nodo representa un plan válido si las unidades aún no asignadas se omiten.
        if covered > best_covered or (covered == best_covered and score < best_score):
            best_covered, best_score, best_assignment = covered, score, assignment[:]
        if index == len(units):
            return

        # Capacities give a stronger upper bound than simply counting remaining wants.
        possible = covered
        for card_key, needed in suffix_counts[index].items():
            stock_left = sum(remaining[i] for i in card_offers.get(card_key, []))
            possible += min(needed, stock_left)
        if possible < best_covered:
            return

        if possible == best_covered:
            need = best_covered - covered
            if need <= 0:
                return
            cheapest = []
            for future in units[index:]:
                demand = demand_rows[future["demand_id"]]
                net_prices = [parsed_offers[i]["price"] - demand["trend"] for i in future["options"]]
                if net_prices:
                    cheapest.append(min(net_prices))
            cheapest.sort()
            if len(cheapest) < need or score + sum(cheapest[:need]) >= best_score:
                return

        unit = units[index]
        available = [i for i in unit["options"] if remaining[i] > 0]
        demand = demand_rows[unit["demand_id"]]
        available.sort(key=lambda i: (
            parsed_offers[i]["price"] - demand["trend"]
            + (0 if parsed_offers[i]["seller_key"] in active else parsed_offers[i]["shipping"]),
            parsed_offers[i]["price"], parsed_offers[i]["seller_key"],
        ))
        for offer_id in available:
            offer = parsed_offers[offer_id]
            new_seller = offer["seller_key"] not in active
            remaining[offer_id] -= 1
            assignment[index] = offer_id
            if new_seller:
                active.add(offer["seller_key"])
            shipping_delta = offer["shipping"] if new_seller else 0
            visit(index + 1, covered + 1, goods_cost + offer["price"],
                  shipping_cost + shipping_delta,
                  score + offer["price"] - demand["trend"] + shipping_delta)
            if new_seller:
                active.remove(offer["seller_key"])
            remaining[offer_id] += 1
            assignment[index] = None
            if limit_reached:
                break

        # If all demand is already covered by the incumbent, skipping cannot improve it.
        if not limit_reached and best_covered < len(units):
            assignment[index] = None
            visit(index + 1, covered, goods_cost, shipping_cost, score)

    visit(0, 0, 0, 0, 0)
    selected = best_assignment

    # Aggregate assigned copies into clear seller/person/card lines.
    grouped: dict[tuple, dict] = {}
    buyer_keys: dict[str, set[str]] = defaultdict(set)
    buyer_displays: dict[str, dict[str, str]] = defaultdict(dict)
    for unit, offer_id in zip(units, selected):
        if offer_id is None:
            continue
        demand = demand_rows[unit["demand_id"]]
        offer = parsed_offers[offer_id]
        key = (offer["seller_key"], demand["person_key"], demand["card_key"], offer_id, demand["trend"])
        if key not in grouped:
            grouped[key] = {
                "seller": offer["seller"], "seller_key": offer["seller_key"],
                "person": demand["person"], "person_key": demand["person_key"],
                "card": demand["card"], "card_key": demand["card_key"],
                "unit_price_cents": offer["price"], "trend_cents": demand["trend"], "quantity": 0,
            }
        grouped[key]["quantity"] += 1
        buyer_keys[offer["seller_key"]].add(demand["person_key"])
        buyer_displays[offer["seller_key"]][demand["person_key"]] = demand["person"]

    details = list(grouped.values())
    seller_ship = {seller: seller_shipping[seller] for seller in buyer_keys}
    # Reparto igualitario entre personas que compran al vendedor; los céntimos sobrantes
    # se asignan en orden alfabético para que el reparto sea determinista y cuadre al céntimo.
    person_ship: dict[tuple[str, str], int] = {}
    for seller, people in buyer_keys.items():
        ordered_people = sorted(people)
        shipping = seller_ship[seller]
        base, remainder_cents = divmod(shipping, len(ordered_people))
        for index, person_key in enumerate(ordered_people):
            person_ship[(seller, person_key)] = base + (1 if index < remainder_cents else 0)

    # Distribuye la parte de envío de cada persona entre sus cartas del vendedor,
    # proporcionalmente al valor de las cartas, para que las filas detalladas concilien.
    by_seller_person: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for line in details:
        line["card_total_cents"] = line["unit_price_cents"] * line["quantity"]
        line["trend_total_cents"] = line["trend_cents"] * line["quantity"]
        by_seller_person[(line["seller_key"], line["person_key"])].append(line)
    for key, lines in by_seller_person.items():
        shipping_share = person_ship[key]
        goods_total = sum(line["card_total_cents"] for line in lines)
        if not goods_total:
            base, remainder_cents = divmod(shipping_share, len(lines))
            for index, line in enumerate(sorted(lines, key=lambda item: item["card_key"])):
                line["shipping_alloc_cents"] = base + (1 if index < remainder_cents else 0)
        else:
            fractions = []
            allocated = 0
            for line in lines:
                numerator = shipping_share * line["card_total_cents"]
                amount, remainder = divmod(numerator, goods_total)
                line["shipping_alloc_cents"] = amount
                allocated += amount
                fractions.append((remainder, line["card_key"], line))
            for _, _, line in sorted(fractions, key=lambda item: (-item[0], item[1]))[:shipping_share - allocated]:
                line["shipping_alloc_cents"] += 1

    for line in details:
        line["delivered_cents"] = line["card_total_cents"] + line["shipping_alloc_cents"]
        line["benefit_cents"] = line["trend_total_cents"] - line["delivered_cents"]

    person_totals: dict[str, dict] = {}
    for line in details:
        person_key = line["person_key"]
        entry = person_totals.setdefault(person_key, {
            "persona": line["person"], "card_total_cents": 0, "trend_total_cents": 0,
            "shipping_cents": 0, "delivered_cents": 0, "benefit_cents": 0,
            "quantity": 0, "sellers": set(),
        })
        entry["card_total_cents"] += line["card_total_cents"]
        entry["trend_total_cents"] += line["trend_total_cents"]
        entry["shipping_cents"] += line["shipping_alloc_cents"]
        entry["delivered_cents"] += line["delivered_cents"]
        entry["benefit_cents"] += line["benefit_cents"]
        entry["quantity"] += line["quantity"]
        entry["sellers"].add(line["seller"])
    people_result = []
    for key in sorted(person_totals):
        entry = person_totals[key]
        entry["sellers"] = sorted(entry["sellers"])
        people_result.append({
            "persona": entry["persona"], "cantidad": entry["quantity"],
            "cartas": _money(entry["card_total_cents"]), "trend": _money(entry["trend_total_cents"]),
            "envio": _money(entry["shipping_cents"]), "total": _money(entry["delivered_cents"]),
            "beneficio": _money(entry["benefit_cents"]), "vendedores": entry["sellers"],
        })

    seller_totals: dict[str, dict] = {}
    for line in details:
        seller_key = line["seller_key"]
        entry = seller_totals.setdefault(seller_key, {
            "vendedor": line["seller"], "card_total_cents": 0, "trend_total_cents": 0,
            "benefit_cents": 0, "quantity": 0, "personas": set(), "cartas": {},
        })
        entry["card_total_cents"] += line["card_total_cents"]
        entry["trend_total_cents"] += line["trend_total_cents"]
        entry["benefit_cents"] += line["trend_total_cents"] - line["card_total_cents"]
        entry["quantity"] += line["quantity"]
        entry["personas"].add(line["person_key"])
        entry["cartas"][line["card"]] = entry["cartas"].get(line["card"], 0) + line["quantity"]
    sellers_result = []
    for key in sorted(seller_totals):
        entry = seller_totals[key]
        people = sorted(entry["personas"])
        shipping = seller_ship[key]
        sellers_result.append({
            "vendedor": entry["vendedor"], "personas": [buyer_displays[key][p] for p in people],
            "lista_cartas": [{"carta": card, "cantidad": quantity} for card, quantity in sorted(entry["cartas"].items())],
            "compradores": len(people), "cantidad": entry["quantity"],
            "cartas": _money(entry["card_total_cents"]), "envio": _money(shipping),
            "total": _money(entry["card_total_cents"] + shipping),
            "trend": _money(entry["trend_total_cents"]),
            "beneficio_sin_envio": _money(entry["benefit_cents"]),
        })

    fulfilled_per_demand = [0] * len(demand_rows)
    for unit, offer_id in zip(units, selected):
        if offer_id is not None:
            fulfilled_per_demand[unit["demand_id"]] += 1
    pending = []
    for row, fulfilled in zip(demand_rows, fulfilled_per_demand):
        if fulfilled < row["quantity"]:
            pending.append({"persona": row["person"], "carta": row["card"],
                            "cantidad": row["quantity"] - fulfilled})

    details_result = []
    for line in sorted(details, key=lambda item: (item["seller_key"], item["person_key"], item["card_key"], item["unit_price_cents"])):
        details_result.append({
            "vendedor": line["seller"], "persona": line["person"], "carta": line["card"],
            "cantidad": line["quantity"], "precio_unitario": _money(line["unit_price_cents"]),
            "trend_unitario": _money(line["trend_cents"]),
            "precio_cartas": _money(line["card_total_cents"]),
            "trend_total": _money(line["trend_total_cents"]),
            "envio_asignado": _money(line["shipping_alloc_cents"]),
            "coste_entregado": _money(line["delivered_cents"]),
            "beneficio": _money(line["benefit_cents"]),
        })

    supplied = best_covered
    total_card_cents = sum(line["card_total_cents"] for line in details)
    total_shipping_cents = sum(seller_ship.values())
    trend_cents = sum(line["trend_total_cents"] for line in details)
    return {
        "optimo_probado": not limit_reached,
        "nodos": nodes,
        "limite_nodos": node_limit,
        "solicitudes": total_units,
        "cubiertas": supplied,
        "sin_cubrir": sum(line["cantidad"] for line in pending),
        "trend": _money(trend_cents),
        "precio_cartas": _money(total_card_cents),
        "envio": _money(total_shipping_cents),
        "total": _money(total_card_cents + total_shipping_cents),
        "beneficio": _money(trend_cents - total_card_cents - total_shipping_cents),
        "vendedores": sellers_result,
        "personas": people_result,
        "compras": details_result,
        "pendientes": pending,
        "regla_envio": "Envío dividido por igual entre las personas que compran a cada vendedor; los céntimos sobrantes se asignan por orden alfabético.",
        "regla_beneficio": "Se prioriza cubrir el máximo de copias y, a igual cobertura, maximizar trend menos cartas y envío. Es orientativo, no una garantía de reventa.",
    }


def _col_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def build_xlsx(result: dict) -> bytes:
    """Genera un XLSX válido con cinco hojas y sin depender de paquetes externos."""
    sheets = [
        ("Resumen", [
            ["Optimizador de compra Cardmarket", ""],
            ["Estado", "Óptimo probado" if result.get("optimo_probado") else "Mejor solución encontrada; límite de búsqueda alcanzado"],
            ["Copias cubiertas", result.get("cubiertas", 0)],
            ["Copias sin cubrir", result.get("sin_cubrir", 0)],
            ["Trend de cartas compradas (€)", result.get("trend", 0)],
            ["Precio de cartas (€)", result.get("precio_cartas", 0)],
            ["Envíos (€)", result.get("envio", 0)],
            ["Total estimado (€)", result.get("total", 0)],
            ["Diferencia frente al trend (€)", result.get("beneficio", 0)],
            ["Nodos examinados", result.get("nodos", 0)],
            ["Reparto de envío", result.get("regla_envio", "")],
            ["Interpretación", result.get("regla_beneficio", "")],
        ]),
        ("Compras", [["Vendedor", "Persona", "Carta", "Cantidad", "Precio unidad (€)", "Trend unidad (€)",
                       "Precio cartas (€)", "Trend total (€)", "Envío asignado (€)", "Coste entregado (€)", "Diferencia (€)"]]
         + [[line.get(key, "") for key in ("vendedor", "persona", "carta", "cantidad", "precio_unitario",
              "trend_unitario", "precio_cartas", "trend_total", "envio_asignado", "coste_entregado", "beneficio")]
            for line in result.get("compras", [])]),
        ("Por persona", [["Persona", "Copias", "Precio cartas (€)", "Trend (€)", "Envío repartido (€)", "Total (€)", "Diferencia (€)", "Vendedores"]]
         + [[line["persona"], line["cantidad"], line["cartas"], line["trend"], line["envio"], line["total"],
             line["beneficio"], ", ".join(line["vendedores"])] for line in result.get("personas", [])]),
        ("Por vendedor", [["Vendedor", "Compradores", "Personas", "Cartas a comprar", "Copias", "Cartas (€)", "Envío (€)", "Total pedido (€)", "Trend (€)", "Diferencia antes de envío (€)"]]
         + [[line["vendedor"], line["compradores"], ", ".join(line["personas"]),
             ", ".join(f'{card["carta"]} × {card["cantidad"]}' for card in line["lista_cartas"]),
             line["cantidad"], line["cartas"], line["envio"], line["total"], line["trend"], line["beneficio_sin_envio"]]
            for line in result.get("vendedores", [])]),
        ("Pendientes", [["Persona", "Carta", "Copias sin vendedor con stock"]]
         + [[line["persona"], line["carta"], line["cantidad"]] for line in result.get("pendientes", [])]),
    ]
    content_types = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
                     '<Default Extension="xml" ContentType="application/xml"/>',
                     '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>']
    for index in range(1, len(sheets) + 1):
        content_types.append(f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
    content_types.append('<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>')
    content_types.append("</Types>")
    workbook_sheets, workbook_rels = [], []
    sheet_xml = {}
    for index, (name, rows) in enumerate(sheets, 1):
        workbook_sheets.append(f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>')
        workbook_rels.append(f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>')
        xml_rows = []
        for row_number, row in enumerate(rows, 1):
            cells = []
            for col_number, value in enumerate(row, 1):
                if value is None:
                    continue
                reference = f"{_col_name(col_number)}{row_number}"
                if isinstance(value, bool):
                    cells.append(f'<c r="{reference}" t="b"><v>{1 if value else 0}</v></c>')
                elif isinstance(value, (int, float)):
                    cells.append(f'<c r="{reference}"><v>{value}</v></c>')
                else:
                    text = escape(str(value)).replace("\r", "")
                    cells.append(f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>')
            xml_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
        sheet_xml[f"xl/worksheets/sheet{index}.xml"] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
            f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>'
        )
    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                f'<sheets>{"".join(workbook_sheets)}</sheets></workbook>')
    package_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                    '</Relationships>')
    workbook_rels.append(f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>')
    workbook_rel_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                        f'{"".join(workbook_rels)}</Relationships>')
    import io
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "".join(content_types))
        archive.writestr("_rels/.rels", package_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rel_xml)
        for path, contents in sheet_xml.items():
            archive.writestr(path, contents)
        workbook_style = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                          '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                          '<fonts count="1"><font><sz val="11"/><name val="Arial"/></font></fonts>'
                          '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
                          '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
                          '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                          '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
                          '</styleSheet>')
        archive.writestr("xl/styles.xml", workbook_style)
    return output.getvalue()


class Handler(BaseHTTPRequestHandler):
    server_version = "CardmarketOptimizer/1.0"

    def _json(self, status: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        origin = self.headers.get("Origin", "")
        if origin.startswith("moz-extension://"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        route = urlparse(self.path).path
        origin = self.headers.get("Origin", "")
        if route in ("/api/import/next", "/api/health") and origin and not _is_same_local_origin(
            origin, self.headers.get("Host", "")
        ):
            self.send_error(403, "Esta aplicación solo acepta peticiones de su origen local.")
            return
        if route == "/api/health":
            self._json(200, {"ok": True, "app": "Cardpilot"})
            return
        if route == "/api/import/next":
            host = self.headers.get("Host", "")
            same_origin = _is_same_local_origin(origin, host)
            same_referrer = _is_same_local_referrer(self.headers.get("Referer", ""), host)
            if not (same_origin or (not origin and same_referrer)):
                self.send_error(403, "La cola solo se consume desde la aplicación local.")
                return
            storage = ROOT / "cardmarket-import.json"
            with IMPORT_QUEUE_LOCK:
                try:
                    current = json.loads(storage.read_text(encoding="utf-8")) if storage.exists() else {"imports": []}
                except (json.JSONDecodeError, OSError):
                    current = {"imports": []}
                imports = current.get("imports", [])
                if imports:
                    last = imports.pop(0)
                    storage.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
                else:
                    last = {"wants": [], "offers": []}
            self._json(200, last)
            return
        if route == "/extension.zip":
            import io
            extension_dir = ROOT / "cardmarket-extension"
            output = io.BytesIO()
            try:
                with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for path in sorted(extension_dir.iterdir()):
                        if path.is_file():
                            archive.write(path, f"cardmarket-extension/{path.name}")
            except OSError:
                self.send_error(500, "No se encuentra la extensión Firefox")
                return
            body = output.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="cardmarket-extension.zip"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            web_root = ROOT / "cardmarket-angular" / "dist" / "cardmarket-angular" / "browser"
            if not (web_root / "index.html").is_file():
                self.send_error(503, "La aplicación Angular aún no está compilada; ejecuta npm run build --prefix cardmarket-angular.")
                return
            requested = (web_root / route.lstrip("/")).resolve() if route != "/" else web_root / "index.html"
            if web_root.resolve() not in requested.resolve().parents and requested.resolve() != web_root.resolve():
                self.send_error(404)
                return
            if not requested.is_file() and Path(route).suffix:
                self.send_error(404)
                return
            body = requested.read_bytes() if requested.is_file() else (web_root / "index.html").read_bytes()
            content_type = "text/html; charset=utf-8" if requested.suffix == ".html" else None
            if requested.suffix == ".js":
                content_type = "text/javascript; charset=utf-8"
            elif requested.suffix == ".css":
                content_type = "text/css; charset=utf-8"
        except OSError:
            self.send_error(500, "No se encuentra cardmarket_optimizador.html")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        origin = self.headers.get("Origin", "")
        self.send_response(204)
        if origin.startswith("moz-extension://"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if route == "/api/import":
            origin = self.headers.get("Origin", "")
            if not origin.startswith("moz-extension://"):
                self.send_error(403, "La importación de extensión solo acepta un origen moz-extension autorizado.")
                return
        if route not in ("/api/optimize", "/api/export", "/api/import"):
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 5_000_000:
                raise ValueError("La solicitud está vacía o supera el límite de 5 MB.")
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if route == "/api/import":
                wants, offers = data.get("wants", []), data.get("offers", [])
                if not isinstance(wants, list) or not isinstance(offers, list):
                    raise ValueError("Wants y ofertas deben ser listas.")
                if len(wants) + len(offers) > 5_000:
                    raise ValueError("La importación supera el límite de filas.")
                if any(not isinstance(row, dict) for row in wants + offers):
                    raise ValueError("Cada fila importada debe ser un objeto.")
                storage = ROOT / "cardmarket-import.json"
                with IMPORT_QUEUE_LOCK:
                    try:
                        current = json.loads(storage.read_text(encoding="utf-8")) if storage.exists() else {"imports": [], "sources": []}
                    except (json.JSONDecodeError, OSError):
                        current = {"imports": [], "sources": []}
                    current.setdefault("imports", []).append({
                        "wants": wants, "offers": offers,
                        "person": str(data.get("person", "")),
                        "pageTitle": str(data.get("pageTitle", "")),
                        "sourceUrl": str(data.get("sourceUrl", "")),
                    })
                    if len(current["imports"]) > 25:
                        current["imports"] = current["imports"][-25:]
                    source_url = str(data.get("sourceUrl", ""))
                    if source_url and source_url not in current.get("sources", []):
                        current.setdefault("sources", []).append(source_url)
                    storage.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
                self._json(200, {"ok": True, "wants": len(wants), "offers": len(offers)})
                return
            if route == "/api/optimize":
                self._json(200, optimize(data.get("wants"), data.get("offers")))
                return
            body = build_xlsx(data)
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", 'attachment; filename="plan_compra_cardmarket.xlsx"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
            self._json(400, {"error": str(error)})
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Abre el optimizador local de compras Cardmarket.")
    parser.add_argument("--port", type=int, default=8765, help="Puerto local (por defecto 8765)")
    parser.add_argument("--no-browser", action="store_true", help="No abrir el navegador automáticamente")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port debe estar entre 1 y 65535")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Optimizador disponible en {url} (solo en este ordenador). Ctrl+C para salir.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
