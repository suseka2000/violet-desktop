"""Portable coherence of native fastening measurements, not CAD re-computation.

The native worker establishes geometric evidence. This validator ensures that
saved evidence cannot be omitted, borrowed from another seat, or contradicted
by the accompanying request/bodies. Fingerprints are integrity, not authority.
"""
import math


def _number(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= 1e12 or not math.isfinite(value):
        raise ValueError("Некорректный результат проверки крепежа: " + name)
    return value


def _finite_coordinate(value):
    return not isinstance(value,bool) and isinstance(value,(int,float)) and -1e9 <= value <= 1e9 and math.isfinite(value)


def validate_fastening_record(spec, bodies, checks, engagements):
    requested = {item["id"]: item for item in spec.get("fastenings", [])}
    if not isinstance(checks, list) or not isinstance(engagements, list) or len(checks) != len(requested):
        raise ValueError("Для каждого соединения крепежом нужна отдельная проверка посадки")
    if any(not isinstance(item.get("id"), str) for item in checks) or len({item.get("id") for item in checks}) != len(requested) or {item.get("id") for item in checks} != set(requested):
        raise ValueError("Проверки крепежа не соответствуют соединениям рецепта")
    body_by_instance = {body["instance_id"]: body for body in bodies}
    body_by_id = {body["id"]: body for body in bodies}
    accepted = []
    for check in checks:
        request = requested[check["id"]]
        if check.get("status") != "valid" or check.get("issues") != []:
            raise ValueError("Соединение крепежом не прошло проверку посадки")
        for key in ("fastener_id", "seat_id", "axis_origin_mm", "axis_direction"):
            if check.get(key) != request[key]:
                raise ValueError("Проверка относится к другой оси или опоре крепежа")
        for key, instance in (("fastener_body_id", request["fastener_id"]), ("seat_body_id", request["seat_id"])):
            if check.get(key) != body_by_instance[instance]["id"]:
                raise ValueError("Проверка крепежа относится к другому телу")
        area = _number(check.get("head_bearing_area_mm2"), "площадь головки", .1)
        contact = _number(check.get("bearing_contact_area_mm2"), "опорная площадь")
        required = _number(check.get("required_bearing_area_mm2"), "требуемая площадь", .1)
        if abs(required-max(.1, area*.25)) > 1e-5 or contact + 1e-5 < required or contact > area + 1e-4:
            raise ValueError("Головка крепежа не имеет достаточной геометрической опоры")
        outside_envelope = _number(check.get("outside_envelope_volume_mm3"), "объём вне осевого габарита")
        envelope_tolerance = _number(check.get("envelope_tolerance_mm3"), "допуск осевого габарита", .001)
        if envelope_tolerance != .001 or outside_envelope > envelope_tolerance:
            raise ValueError("Геометрия крепежа не соответствует проверенной оси")
        span = check.get("shaft_span_mm")
        if not isinstance(span, list) or len(span) != 2 or any(not _finite_coordinate(v) for v in span) or abs(span[1]-request["shaft_length_mm"]) > .05 or abs(span[0]) > .05:
            raise ValueError("Измеренная длина стержня не соответствует крепежу")
        measured = check.get("engagements")
        targets = request["engagement_targets"]
        if not isinstance(measured, list) or len(measured) != len(targets) or any(not isinstance(item,dict) or not isinstance(item.get("target_id"),str) for item in measured) or {item.get("target_id") for item in measured} != set(targets):
            raise ValueError("Крепёж не проверен во всех заданных деталях")
        for item in measured:
            if item.get("status") != "valid" or item.get("fastening_id") != request["id"] or item.get("fastener_id") != request["fastener_id"]:
                raise ValueError("Неверная принадлежность или статус зацепления крепежа")
            a, b = item.get("body_a"), item.get("body_b")
            if type(a) is not int or type(b) is not int or a not in body_by_id or b not in body_by_id or a == b or {body_by_id[a]["instance_id"],body_by_id[b]["instance_id"]} != {request["fastener_id"],item["target_id"]}:
                raise ValueError("Зацепление записано для другой пары тел")
            volume = _number(item.get("volume_mm3"), "объём зацепления")
            outside = _number(item.get("outside_shaft_volume_mm3"), "объём вне стержня")
            depth = _number(item.get("depth_mm"), "глубина зацепления")
            if item.get("min_engagement_mm") != request["min_engagement_mm"] or volume <= .001 or outside > .001 or depth + 1e-5 < request["min_engagement_mm"]:
                raise ValueError("Недостаточная глубина либо пересечение вне стержня крепежа")
            axial = item.get("axial_span_mm")
            if not isinstance(axial,list) or len(axial) != 2 or any(not _finite_coordinate(v) for v in axial) or axial[0] < -.002 or axial[1] > request["shaft_length_mm"]+.002 or axial[1]-axial[0]+1e-5 < depth:
                raise ValueError("Осевой интервал зацепления выходит за стержень крепежа")
            accepted.append(item)
    if any(not isinstance(item,dict) or not isinstance(item.get("fastening_id"),str) or not isinstance(item.get("target_id"),str) for item in engagements) or len(accepted) != len(engagements) or sorted(accepted,key=lambda item:(item["fastening_id"],item["target_id"])) != sorted(engagements,key=lambda item:(item["fastening_id"],item["target_id"])):
        raise ValueError("Реестр допустимых зацеплений не соответствует проверкам крепежа")


def validate_contact_record(spec, bodies, checks):
    requested = {item["id"]: item for item in spec.get("contacts", [])}
    if not isinstance(checks,list) or len(checks)!=len(requested) or any(not isinstance(item,dict) or not isinstance(item.get("id"),str) for item in checks) or {item["id"] for item in checks}!=set(requested):
        raise ValueError("Не все опорные контакты сборки проверены")
    by_instance = {body["instance_id"]:body for body in bodies}
    for check in checks:
        request=requested[check["id"]]
        if check.get("status")!="valid" or check.get("issues")!=[]:
            raise ValueError("Детали сборки не имеют проверенного опорного контакта")
        for field in ("body_a","body_b"):
            if check.get(field)!=request[field] or check.get(field+"_id")!=by_instance[request[field]]["id"]:
                raise ValueError("Проверка контакта относится к другим деталям")
        area=_number(check.get("contact_area_mm2"),"площадь контакта")
        volume=_number(check.get("overlap_volume_mm3"),"объём пересечения в контакте")
        if check.get("min_area_mm2")!=request["min_area_mm2"] or area+1e-5<request["min_area_mm2"] or volume>.001:
            raise ValueError("Недостаточная площадь контакта либо объёмное пересечение деталей")


def validate_connected_assembly(bodies, contacts, engagements):
    """All bodies must join one measured contact/engagement network.

    Valid screws in a detached backdrop must not certify a floating profile or
    panel. Ordinary volume collisions are never used as connectivity evidence.
    """
    graph = {body["id"]:set() for body in bodies}
    for a,b in [(item["body_a_id"],item["body_b_id"]) for item in contacts or []]+[(item["body_a"],item["body_b"]) for item in engagements or []]:
        graph[a].add(b);graph[b].add(a)
    reached, pending = set(), [next(iter(graph))]
    while pending:
        current=pending.pop()
        if current in reached:continue
        reached.add(current);pending.extend(graph[current]-reached)
    if reached != set(graph):
        missing=", ".join(body["instance_id"] for body in bodies if body["id"] not in reached)
        raise ValueError("В 3D-сборке есть несвязанные детали без проверенной опоры: " + missing)
