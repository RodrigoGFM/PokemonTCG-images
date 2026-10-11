"""
Calcula el meta de Pokémon TCG (Standard) con la API de Limitless y lo guarda en meta/meta.json,
que la app Deck & Dex descarga para su sección "Meta" de Decks.

Fuente: https://docs.limitlesstcg.com/developer/tournaments.html -- solo torneos ONLINE de
play.limitlesstcg.com (los Regionales presenciales no están en esa API).

La clave de Limitless es opcional: si existe la variable de entorno LIMITLESS_KEY (un secret del
repo) se manda en la cabecera X-Access-Key. Sin clave el límite es 50 pedidos cada 5 minutos;
si se llega a él, el script espera y reintenta.

El formato del JSON tiene que coincidir con MetaLimitless.Resumen de la app. Si se cambia de forma
incompatible, subir VERSION (la app ignora versiones que no conoce).
"""

import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

VERSION = 1
BASE = "https://play.limitlesstcg.com/api"
SALIDA = os.path.join(os.path.dirname(__file__), "..", "meta", "meta.json")
DIAS = 14
MAX_PAGINAS = 10
TORNEOS_A_ANALIZAR = 20
MIN_JUGADORES = 64
# Arquetipos que cuentan como "meta" para el win rate contra el meta / contra el resto.
ARQUETIPOS_META = 8
# Se listan los arquetipos con al menos esta representación (y nunca "other").
REPRESENTACION_MINIMA = 0.01
ID_OTROS = "other"
CORTES_TOP = [1, 2, 4, 8, 16, 32, 64]
# Cartas más usadas: solo las que aparecen en al menos esta fracción de las listas del arquetipo.
INCLUSION_MINIMA = 0.10
# Listas completas por arquetipo (las mejor ubicadas), para importarlas desde la app.
LISTAS_POR_ARQUETIPO = 5
SECCIONES = ["pokemon", "trainer", "energy"]

sesion = requests.Session()
sesion.headers["Accept"] = "application/json"
if os.environ.get("LIMITLESS_KEY"):
    sesion.headers["X-Access-Key"] = os.environ["LIMITLESS_KEY"]


def pedir(ruta, reintentos=3):
    for _ in range(reintentos):
        r = sesion.get(BASE + ruta, timeout=60)
        if r.status_code == 429:
            print("Límite de pedidos alcanzado, esperando 5 minutos...", flush=True)
            time.sleep(305)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Limitless sigue rechazando {ruta}")


def win_rate(victorias, empates, partidas):
    """Los empates cuentan como media victoria (la cuenta que usan los reportes de @poke_hive)."""
    return 0.0 if partidas == 0 else (victorias + empates / 2) / partidas


def torneos_recientes():
    limite = datetime.now(timezone.utc) - timedelta(days=DIAS)
    torneos = []
    for pagina in range(1, MAX_PAGINAS + 1):
        lote = pedir(f"/tournaments?game=PTCG&format=STANDARD&limit=100&page={pagina}")
        if not lote:
            break
        torneos += lote
        ultima = datetime.fromisoformat(lote[-1]["date"].replace("Z", "+00:00"))
        if ultima < limite:
            break
    torneos = [
        t for t in torneos
        if t.get("players", 0) >= MIN_JUGADORES
        and datetime.fromisoformat(t["date"].replace("Z", "+00:00")) >= limite
    ]
    return sorted(torneos, key=lambda t: -t["players"])[:TORNEOS_A_ANALIZAR]


def cartas_mas_usadas(listas):
    """
    Qué cartas juega el arquetipo: en qué fracción de las listas aparece cada una y cuántas copias
    se suelen poner. Los Pokémon se distinguen por impresión (dos "Dunsparce" distintos son cartas
    distintas); Entrenadores y Energías por nombre (una reimpresión de Ultra Ball es la misma carta),
    mostrando la impresión más usada.
    """
    if not listas:
        return []
    grupos = {}
    for lista in listas:
        por_clave = defaultdict(int)
        impresion = {}
        for c in lista["cartas"]:
            clave = (c["seccion"], c["nombre"], c["set"], c["numero"]) if c["seccion"] == "pokemon" else (c["seccion"], c["nombre"])
            por_clave[clave] += c["cantidad"]
            impresion[clave] = (c["set"], c["numero"])
        for clave, cantidad in por_clave.items():
            g = grupos.setdefault(clave, {"cantidades": [], "impresiones": defaultdict(int)})
            g["cantidades"].append(cantidad)
            g["impresiones"][impresion[clave]] += 1
    cartas = []
    for clave, g in grupos.items():
        inclusion = len(g["cantidades"]) / len(listas)
        if inclusion < INCLUSION_MINIMA:
            continue
        set_, numero = max(g["impresiones"].items(), key=lambda kv: kv[1])[0]
        cantidades = g["cantidades"]
        cartas.append({
            "nombre": clave[1], "set": set_, "numero": numero, "seccion": clave[0],
            "inclusion": inclusion,
            "copias": max(set(cantidades), key=lambda n: (cantidades.count(n), n)),
            "promedio": sum(cantidades) / len(cantidades),
        })
    cartas.sort(key=lambda c: (SECCIONES.index(c["seccion"]), -c["inclusion"], -c["copias"], c["nombre"]))
    return cartas


def mejores_listas(listas):
    """Las mejor ubicadas: primero las que llegaron al top cut, después por puesto y tamaño del torneo."""
    ordenadas = sorted(listas, key=lambda l: (not l["enTopCut"], l["puesto"] or 9999, -l["jugadoresTorneo"]))
    return ordenadas[:LISTAS_POR_ARQUETIPO]


def calcular():
    torneos = torneos_recientes()
    if not torneos:
        raise RuntimeError("No hay torneos recientes")

    acumulados = {}

    def acumulado(deck_id):
        if deck_id not in acumulados:
            acumulados[deck_id] = {
                "nombre": deck_id, "iconos": [], "jugadores": 0, "enTopCut": 0,
                "jugadoresConCut": 0, "distribucion": defaultdict(int),
                # Rival -> [victorias, derrotas, empates]
                "matchups": defaultdict(lambda: [0, 0, 0]),
                "listas": [],
            }
        return acumulados[deck_id]

    nombres = {ID_OTROS: "Other"}
    jugadores_totales = jugadores_con_cut = en_top_cut_total = 0

    for t in torneos:
        standings = pedir(f"/tournaments/{t['id']}/standings")
        pairings = pedir(f"/tournaments/{t['id']}/pairings")
        print(f"  {t['players']:5} jugadores · {t['name']}", flush=True)

        # El top cut son las fases después del suizo (phase > 1).
        en_cut = {p for x in pairings if (x.get("phase") or 1) > 1 for p in (x.get("player1"), x.get("player2")) if p}

        deck_de = {}
        for s in standings:
            deck = s.get("deck") or {}
            deck_id = deck.get("id") or ID_OTROS
            deck_de[s["player"]] = deck_id
            acc = acumulado(deck_id)
            if deck.get("name"):
                acc["nombre"] = nombres[deck_id] = deck["name"]
            if deck.get("icons"):
                acc["iconos"] = deck["icons"]
            acc["jugadores"] += 1
            lugar = s.get("placing")
            if lugar:
                corte = next((c for c in CORTES_TOP if lugar <= c), None)
                if corte:
                    acc["distribucion"][corte] += 1
            if s.get("decklist"):
                record = s.get("record") or {}
                acc["listas"].append({
                    "jugador": s.get("name") or s["player"],
                    "torneo": t["name"],
                    "fecha": t["date"][:10],
                    "puesto": lugar,
                    "jugadoresTorneo": t["players"],
                    "victorias": record.get("wins", 0),
                    "derrotas": record.get("losses", 0),
                    "empates": record.get("ties", 0),
                    "enTopCut": s["player"] in en_cut,
                    "cartas": [
                        {"cantidad": c["count"], "nombre": c["name"], "set": c.get("set", ""), "numero": str(c.get("number", "")), "seccion": seccion}
                        for seccion in SECCIONES for c in s["decklist"].get(seccion, [])
                    ],
                })
        jugadores_totales += len(deck_de)

        if en_cut:
            jugadores_con_cut += len(deck_de)
            en_top_cut_total += sum(1 for p in en_cut if p in deck_de)
            for jugador, deck_id in deck_de.items():
                acumulados[deck_id]["jugadoresConCut"] += 1
                if jugador in en_cut:
                    acumulados[deck_id]["enTopCut"] += 1

        for x in pairings:
            a, b = x.get("player1"), x.get("player2")
            if not a or not b:
                continue  # bye
            ganador = str(x.get("winner"))
            deck_a, deck_b = deck_de.get(a, ID_OTROS), deck_de.get(b, ID_OTROS)
            if deck_a == deck_b:
                continue  # espejo: no suma al win rate
            for yo, mio, rival in ((a, deck_a, deck_b), (b, deck_b, deck_a)):
                r = acumulado(mio)["matchups"][rival]
                if ganador == yo:
                    r[0] += 1
                elif ganador == "0":
                    r[2] += 1
                else:
                    r[1] += 1  # ganó el rival, o derrota doble (-1)

    conversion_campo = en_top_cut_total / jugadores_con_cut if jugadores_con_cut else None
    ordenados = sorted((k for k in acumulados if k != ID_OTROS), key=lambda k: -acumulados[k]["jugadores"])
    meta = set(ordenados[:ARQUETIPOS_META])

    arquetipos = []
    for deck_id in ordenados:
        acc = acumulados[deck_id]
        if acc["jugadores"] / jugadores_totales < REPRESENTACION_MINIMA:
            continue
        matchups = [
            {"rivalId": rival, "rivalNombre": nombres.get(rival, rival), "victorias": w, "derrotas": l, "empates": e}
            for rival, (w, l, e) in acc["matchups"].items()
        ]
        matchups.sort(key=lambda m: -(m["victorias"] + m["derrotas"] + m["empates"]))

        def wr_de(filtro):
            m = [x for x in matchups if filtro(x)]
            partidas = sum(x["victorias"] + x["derrotas"] + x["empates"] for x in m)
            return win_rate(sum(x["victorias"] for x in m), sum(x["empates"] for x in m), partidas) if partidas else None

        conversion = acc["enTopCut"] / acc["jugadoresConCut"] if acc["jugadoresConCut"] else None
        arquetipos.append({
            "id": deck_id,
            "nombre": acc["nombre"],
            "iconos": acc["iconos"],
            "jugadores": acc["jugadores"],
            "representacion": acc["jugadores"] / jugadores_totales,
            "representacionTopCut": acc["enTopCut"] / en_top_cut_total if en_top_cut_total else 0.0,
            "victorias": sum(m["victorias"] for m in matchups),
            "derrotas": sum(m["derrotas"] for m in matchups),
            "empates": sum(m["empates"] for m in matchups),
            "enTopCut": acc["enTopCut"],
            "jugadoresConCut": acc["jugadoresConCut"],
            "rendimientoConversion": conversion / conversion_campo - 1 if conversion is not None and conversion_campo else None,
            "winRateMeta": wr_de(lambda m: m["rivalId"] in meta),
            "winRateOtros": wr_de(lambda m: m["rivalId"] not in meta),
            "distribucionTop": {str(k): v for k, v in sorted(acc["distribucion"].items())},
            "matchups": matchups,
            "cartas": cartas_mas_usadas(acc["listas"]),
            "listas": mejores_listas(acc["listas"]),
        })

    fechas = sorted(t["date"][:10] for t in torneos)
    return {
        "version": VERSION,
        "calculadoEn": int(time.time() * 1000),
        "torneos": [t["name"] for t in torneos],
        "jugadores": jugadores_totales,
        "desde": fechas[0],
        "hasta": fechas[-1],
        "arquetipos": arquetipos,
    }


def redondear(valor):
    """4 decimales alcanzan para mostrar porcentajes con un decimal, y achican bastante el JSON."""
    if isinstance(valor, float):
        return round(valor, 4)
    if isinstance(valor, dict):
        return {k: redondear(v) for k, v in valor.items()}
    if isinstance(valor, list):
        return [redondear(v) for v in valor]
    return valor


def main():
    resumen = redondear(calcular())
    os.makedirs(os.path.dirname(SALIDA), exist_ok=True)
    with open(SALIDA, "w", encoding="utf-8") as f:
        json.dump(resumen, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(resumen['torneos'])} torneos, {resumen['jugadores']} jugadores, "
          f"{len(resumen['arquetipos'])} arquetipos ({resumen['desde']} a {resumen['hasta']})")


if __name__ == "__main__":
    sys.exit(main())
