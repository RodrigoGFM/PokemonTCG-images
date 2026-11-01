"""
Calcula el metajuego de Pokémon TCG de los torneos PRESENCIALES oficiales (Regionales,
Internacionales, Special Events y Mundiales de Play! Pokémon) y lo guarda en
meta/meta_regionales.json, con el mismo formato que meta/meta.json (el meta online), para que
Deck & Dex muestre los dos.

Los datos salen de Limitless Labs (labs.limitlesstcg.com), que publica de cada torneo el deck, el
puesto y el récord de casi todos los jugadores, cada emparejamiento ronda por ronda con el deck de
los dos jugadores, y las listas. La API es la misma que usa esa página.

Igual que el meta online (actualizar_meta.py), salvo:
- Solo se usan los torneos de la temporada más reciente, para que todo sea del formato actual.
- El "top cut" es el Day 2: el Top 8 de un Regional deja 8 lugares entre más de 2000 jugadores, y
  la conversión saldría casi toda en -100 %. El Day 2 es el corte que separa a ~1 de cada 6.
- La tendencia compara la mitad más nueva de los torneos con la más vieja (en vez de semanas).

Además, de cada torneo se guarda meta/torneos/<id>.json con lo mismo que muestra su página de
Labs: la clasificación completa (puesto, jugador, país, récord, deck, Day 2), los decks del torneo
con su win rate, y las listas de los mejor ubicados. El índice va en "eventos" de meta_regionales.json.
"""

import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Mismos criterios que el meta online: cartas más usadas, mejores listas, redondeo y win rate.
from actualizar_meta import ID_OTROS, cartas_mas_usadas, mejores_listas, redondear, win_rate  # noqa: E402

VERSION = 1
LABS = "https://labs.limitlesstcg.com"
API = "https://mew.limitlesstcg.com/labs/data/tcg"
SALIDA = os.path.join(os.path.dirname(__file__), "..", "meta", "meta_regionales.json")
CARPETA_TORNEOS = os.path.join(os.path.dirname(__file__), "..", "meta", "torneos")
# Listas que se guardan por torneo (las de los mejor ubicados), para verlas en la app.
LISTAS_POR_TORNEO = 32

MAX_TORNEOS = 8
DIVISION = "MA"  # Masters
CORTES_TOP = [1, 2, 4, 8, 16, 32, 64]
# Arquetipos que cuentan como "meta" para el win rate contra el meta / contra el resto.
ARQUETIPOS_META = 8
# Se listan los arquetipos con al menos esta representación (y nunca "other").
REPRESENTACION_MINIMA = 0.01
# Listas que se descargan por arquetipo (las mejor ubicadas) para calcular las cartas más usadas.
LISTAS_PARA_CARTAS = 30
SECCIONES = ["pokemon", "trainer", "energy"]
PAUSA = 0.25
MESES = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}

sesion = requests.Session()
sesion.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
})


def pedir(url, reintentos=3):
    for i in range(reintentos):
        try:
            r = sesion.get(url, timeout=60)
            if r.status_code == 429:
                print("Límite de solicitudes alcanzado, esperando 20 segundos...", flush=True)
                time.sleep(20)
                continue
            r.raise_for_status()
            return r
        except Exception as e:
            if i == reintentos - 1:
                print(f"Error al pedir {url}: {e}", file=sys.stderr)
                return None
            time.sleep(2)
    return None


def api(ruta):
    """El campo "message" de una respuesta de la API de Labs, o None si falló."""
    r = pedir(f"{API}/{ruta}")
    time.sleep(PAUSA)
    if r is None:
        return None
    try:
        datos = r.json()
    except ValueError:
        return None
    return datos.get("message") if datos.get("ok") else None


def fecha_iso(texto):
    """'October 24–25, 2026' o 'August 28–30, 2026' -> '2026-10-24' (el primer día)."""
    m = re.match(r"([A-Za-z]+)\s+(\d{1,2})\D.*?(\d{4})", texto or "")
    if not m or m.group(1).lower() not in MESES:
        return ""
    return f"{m.group(3)}-{MESES[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"


def nombre_torneo(t):
    tipo = {"regional": "Regional", "international": "International", "worlds": "Worlds",
            "special": "Special Event"}.get(t.get("type"), (t.get("type") or "").title())
    return t.get("name") or f"{tipo} {t.get('city') or ''}".strip()


def torneos_recientes():
    """Los torneos terminados de la temporada más reciente, del más nuevo al más viejo."""
    r = pedir(LABS)
    if r is None:
        return []
    ids = list(dict.fromkeys(re.findall(r'href="/(\d{4})/standings"', r.text)))
    torneos = []
    temporada = None
    for tid in ids:
        t = api(f"tournament?id={tid}&division={DIVISION}")
        if not t or not t.get("completed"):
            continue
        if temporada is None:
            temporada = t.get("season")
        if t.get("season") != temporada:
            break
        torneos.append({
            "id": tid,
            "nombre": nombre_torneo(t),
            "tipo": t.get("type") or "",
            "ciudad": t.get("city") or "",
            "pais": t.get("country") or "",
            "fechaTexto": t.get("date") or "",
            "fecha": fecha_iso(t.get("date")),
            "jugadores": t.get("players") or 0,
            "rondas": t.get("round") or 0,
            "listas": bool(t.get("decklists")),
        })
        if len(torneos) >= MAX_TORNEOS:
            break
    return torneos


_listas = {}


def lista_de(torneo_id, jugador_id):
    """Las cartas de una lista (con caché: el meta y el detalle del torneo piden algunas iguales)."""
    clave = (torneo_id, jugador_id)
    if clave not in _listas:
        lista = api(f"decklist?tournamentId={torneo_id}&playerId={jugador_id}")
        _listas[clave] = [
            {"cantidad": k["count"], "nombre": k["name"], "set": k.get("set", ""),
             "numero": str(k.get("number", "")), "seccion": seccion}
            for seccion in SECCIONES for k in (lista or {}).get(seccion, [])
        ]
    return _listas[clave]


def detalle_torneo(t, standings, decks_partidas):
    """Lo que muestra la página del torneo en Labs: clasificación, decks y las mejores listas."""
    decks = {}
    for s in standings:
        deck_id = s.get("deck_id") or ID_OTROS
        d = decks.setdefault(deck_id, {
            "id": deck_id, "nombre": s.get("deck_name") or ("Other" if deck_id == ID_OTROS else deck_id),
            "iconos": (s.get("icons") or "").split(), "jugadores": 0, "day2": 0, "topCut": 0,
            "victorias": 0, "derrotas": 0, "empates": 0,
        })
        d["jugadores"] += 1
        d["day2"] += 1 if s.get("day2") else 0
        d["topCut"] += 1 if s.get("topcut") else 0
    for deck_id, (w, l, e) in decks_partidas.items():
        if deck_id in decks:
            decks[deck_id].update({"victorias": w, "derrotas": l, "empates": e})
    # Los números de la pestaña Decks de Labs, para que se vean iguales: su récord cuenta los
    # espejos, y sus jugadores no incluyen a los que no tienen deck registrado.
    jugadores_con_deck = sum(1 for s in standings if s.get("deck_id"))
    labs = api(f"decks?tournamentId={t['id']}&division={DIVISION}") or []
    for x in labs:
        d = decks.get(x.get("identifier"))
        if d is None:
            continue
        d.update({"jugadores": x.get("players") or 0, "day2": x.get("day2s") or 0,
                  "victorias": x.get("wins") or 0, "derrotas": x.get("losses") or 0, "empates": x.get("ties") or 0})
    if labs:
        jugadores_con_deck = sum(x.get("players") or 0 for x in labs)
        # Los que no tienen deck en Labs no aparecen en su pestaña Decks.
        if ID_OTROS in decks and not any(x.get("identifier") == ID_OTROS for x in labs):
            del decks[ID_OTROS]

    jugadores = []
    con_lista = 0
    for s in sorted(standings, key=lambda s: s.get("placement") or 99999):
        fila = {
            "puesto": s.get("placement"),
            "nombre": s.get("name") or "",
            "pais": s.get("country") or "",
            "puntos": s.get("points") or 0,
            "victorias": s.get("wins") or 0,
            "derrotas": s.get("losses") or 0,
            "empates": s.get("ties") or 0,
            "day2": bool(s.get("day2")),
            "topCut": bool(s.get("topcut")),
            "abandono": s.get("drop_round"),
            "deck": s.get("deck_id") or ID_OTROS,
        }
        if con_lista < LISTAS_POR_TORNEO and t["listas"] and s.get("decklist") and s.get("tp_id") is not None:
            cartas = lista_de(t["id"], s["tp_id"])
            if cartas:
                fila["lista"] = cartas
                con_lista += 1
        jugadores.append(fila)

    return {
        "version": VERSION,
        "calculadoEn": int(time.time() * 1000),
        "id": t["id"],
        "nombre": t["nombre"],
        "tipo": t["tipo"],
        "ciudad": t["ciudad"],
        "pais": t["pais"],
        "fecha": t["fecha"],
        "fechaTexto": t["fechaTexto"],
        "jugadores": len(standings),
        # La base del porcentaje de cada deck, como en Labs.
        "jugadoresConDeck": jugadores_con_deck,
        "rondas": t["rondas"],
        "url": f"{LABS}/{t['id']}/standings",
        "decks": sorted(decks.values(), key=lambda d: -d["jugadores"]),
        "clasificacion": jugadores,
    }


def guardar_torneo(detalle):
    os.makedirs(CARPETA_TORNEOS, exist_ok=True)
    with open(os.path.join(CARPETA_TORNEOS, f"{detalle['id']}.json"), "w", encoding="utf-8") as f:
        json.dump(redondear(detalle), f, ensure_ascii=False, separators=(",", ":"))


def calcular():
    torneos = torneos_recientes()
    if not torneos:
        raise RuntimeError("No se encontraron torneos presenciales recientes")

    acumulados = {}

    def acumulado(deck_id):
        if deck_id not in acumulados:
            acumulados[deck_id] = {
                "nombre": deck_id, "iconos": [], "jugadores": 0, "enTopCut": 0,
                "recientes": 0, "previos": 0, "distribucion": defaultdict(int),
                # Rival -> [victorias, derrotas, empates]
                "matchups": defaultdict(lambda: [0, 0, 0]),
                "candidatas": [],
            }
        return acumulados[deck_id]

    nombres = {ID_OTROS: "Other"}
    jugadores_totales = en_top_cut_total = partidas_contadas = 0
    ids_recientes = {t["id"] for t in torneos[: (len(torneos) + 1) // 2]}
    jugadores_recientes = jugadores_previos = 0

    for t in torneos:
        standings = api(f"standings?tournamentId={t['id']}&division={DIVISION}")
        if not standings:
            print(f"  (sin standings) {t['nombre']}", flush=True)
            continue
        es_reciente = t["id"] in ids_recientes
        con_deck = 0
        for s in standings:
            deck_id = s.get("deck_id") or ID_OTROS
            acc = acumulado(deck_id)
            if s.get("deck_id"):
                con_deck += 1
                acc["nombre"] = nombres[deck_id] = s.get("deck_name") or deck_id
                if s.get("icons"):
                    acc["iconos"] = s["icons"].split()
            acc["jugadores"] += 1
            if es_reciente:
                acc["recientes"] += 1
            else:
                acc["previos"] += 1
            lugar = s.get("placement")
            if lugar:
                corte = next((c for c in CORTES_TOP if lugar <= c), None)
                if corte:
                    acc["distribucion"][corte] += 1
            if s.get("day2"):
                acc["enTopCut"] += 1
                en_top_cut_total += 1
            if s.get("decklist") and t["listas"] and s.get("deck_id"):
                acc["candidatas"].append({
                    "jugador": s.get("name") or "",
                    "torneo": t["nombre"],
                    "fecha": t["fecha"],
                    "puesto": lugar,
                    "jugadoresTorneo": t["jugadores"],
                    "victorias": s.get("wins") or 0,
                    "derrotas": s.get("losses") or 0,
                    "empates": s.get("ties") or 0,
                    "enTopCut": bool(s.get("day2")),
                    "_torneo": t["id"],
                    "_jugador": s.get("tp_id"),  # el id de jugador del torneo (el de la página), no player_id
                })
        n = len(standings)
        jugadores_totales += n
        if es_reciente:
            jugadores_recientes += n
        else:
            jugadores_previos += n

        partidas_torneo = 0
        # Deck -> [victorias, derrotas, empates] en este torneo (para el detalle del torneo).
        decks_partidas = defaultdict(lambda: [0, 0, 0])
        for ronda in range(1, t["rondas"] + 1):
            for x in api(f"pairings?tournamentId={t['id']}&division={DIVISION}&round={ronda}") or []:
                a, b, ganador = x.get("player1"), x.get("player2"), x.get("winner")
                if not a or not b or ganador is None or not x.get("completed"):
                    continue  # bye o partida sin resultado
                if ganador not in (a, b, 0, -1):
                    continue
                deck_a, deck_b = x.get("p1_deck") or ID_OTROS, x.get("p2_deck") or ID_OTROS
                if deck_a == deck_b:
                    continue  # espejo: no suma al win rate
                partidas_torneo += 1
                for yo, mio, rival in ((a, deck_a, deck_b), (b, deck_b, deck_a)):
                    # 0: victoria, 2: empate, 1: ganó el rival o derrota doble (-1)
                    i = 0 if ganador == yo else 2 if ganador == 0 else 1
                    acumulado(mio)["matchups"][rival][i] += 1
                    decks_partidas[mio][i] += 1
        partidas_contadas += partidas_torneo
        try:
            guardar_torneo(detalle_torneo(t, standings, decks_partidas))
            t["guardado"] = True
        except Exception as e:  # el detalle de un torneo no frena el meta
            print(f"  (no se pudo guardar el detalle) {t['nombre']}: {e}", file=sys.stderr)
        print(f"  {t['fecha']} · {n:5} jugadores · {con_deck} con deck · {partidas_torneo} partidas · {t['nombre']}", flush=True)

    if not jugadores_totales:
        raise RuntimeError("No se pudo leer ningún torneo")
    if not partidas_contadas:
        raise RuntimeError("No se pudo leer ninguna partida: no se sobrescribe el meta anterior")

    conversion_campo = en_top_cut_total / jugadores_totales
    ordenados = sorted((k for k in acumulados if k != ID_OTROS), key=lambda k: -acumulados[k]["jugadores"])
    meta = set(ordenados[:ARQUETIPOS_META])

    arquetipos = []
    for deck_id in ordenados:
        acc = acumulados[deck_id]
        if acc["jugadores"] / jugadores_totales < REPRESENTACION_MINIMA:
            continue

        # Listas de los mejor ubicados (por puesto y, entre puestos iguales, del torneo más grande).
        candidatas = sorted(acc["candidatas"], key=lambda l: (l["puesto"] or 9999, -l["jugadoresTorneo"]))
        listas = []
        for c in candidatas[:LISTAS_PARA_CARTAS]:
            cartas = lista_de(c["_torneo"], c["_jugador"])
            if cartas:
                listas.append({k: v for k, v in c.items() if not k.startswith("_")} | {"cartas": cartas})

        matchups = [
            {"rivalId": rival, "rivalNombre": nombres.get(rival, rival), "victorias": w, "derrotas": l, "empates": e}
            for rival, (w, l, e) in acc["matchups"].items()
        ]
        matchups.sort(key=lambda m: -(m["victorias"] + m["derrotas"] + m["empates"]))

        def wr_de(filtro):
            victorias = sum(x["victorias"] for x in matchups if filtro(x))
            empates = sum(x["empates"] for x in matchups if filtro(x))
            derrotas = sum(x["derrotas"] for x in matchups if filtro(x))
            partidas = victorias + derrotas + empates
            return win_rate(victorias, empates, partidas) if partidas else None

        conversion = acc["enTopCut"] / acc["jugadores"]
        arquetipos.append({
            "id": deck_id,
            "nombre": acc["nombre"],
            "iconos": acc["iconos"],
            "jugadores": acc["jugadores"],
            "representacion": acc["jugadores"] / jugadores_totales,
            "representacionTopCut": acc["enTopCut"] / en_top_cut_total if en_top_cut_total else 0.0,
            "representacionReciente": acc["recientes"] / jugadores_recientes if jugadores_recientes else None,
            "representacionPrevia": acc["previos"] / jugadores_previos if jugadores_previos else None,
            "victorias": sum(m["victorias"] for m in matchups),
            "derrotas": sum(m["derrotas"] for m in matchups),
            "empates": sum(m["empates"] for m in matchups),
            "enTopCut": acc["enTopCut"],
            "jugadoresConCut": acc["jugadores"],
            "rendimientoConversion": conversion / conversion_campo - 1 if conversion_campo else None,
            "winRateMeta": wr_de(lambda m: m["rivalId"] in meta),
            "winRateOtros": wr_de(lambda m: m["rivalId"] not in meta),
            "distribucionTop": {str(k): v for k, v in sorted(acc["distribucion"].items())},
            "matchups": matchups,
            "cartas": cartas_mas_usadas(listas),
            "listas": mejores_listas(listas),
        })
        print(f"  {acc['jugadores']:5} jugadores · {len(listas):2} listas · {acc['nombre']}", flush=True)

    fechas = sorted(t["fecha"] for t in torneos if t["fecha"])
    return {
        "version": VERSION,
        "calculadoEn": int(time.time() * 1000),
        "torneos": [t["nombre"] for t in torneos],
        "jugadores": jugadores_totales,
        "desde": fechas[0] if fechas else "",
        "hasta": fechas[-1] if fechas else datetime.now().strftime("%Y-%m-%d"),
        "arquetipos": arquetipos,
        # Índice de los torneos con detalle en meta/torneos/<id>.json, del más nuevo al más viejo.
        "eventos": [
            {"id": t["id"], "nombre": t["nombre"], "tipo": t["tipo"], "ciudad": t["ciudad"], "pais": t["pais"],
             "fecha": t["fecha"], "fechaTexto": t["fechaTexto"], "jugadores": t["jugadores"]}
            for t in torneos if t.get("guardado")
        ],
    }


def main():
    print("Calculando el meta de torneos presenciales (Limitless Labs)...", flush=True)
    resumen = redondear(calcular())
    os.makedirs(os.path.dirname(SALIDA), exist_ok=True)
    with open(SALIDA, "w", encoding="utf-8") as f:
        json.dump(resumen, f, ensure_ascii=False, separators=(",", ":"))
    # Los detalles de torneos que ya no están en el índice se borran.
    vigentes = {f"{e['id']}.json" for e in resumen["eventos"]}
    if os.path.isdir(CARPETA_TORNEOS):
        for nombre in os.listdir(CARPETA_TORNEOS):
            if nombre.endswith(".json") and nombre not in vigentes:
                os.remove(os.path.join(CARPETA_TORNEOS, nombre))
    print(f"{len(resumen['torneos'])} torneos, {resumen['jugadores']} jugadores, "
          f"{len(resumen['arquetipos'])} arquetipos ({resumen['desde']} a {resumen['hasta']})")


if __name__ == "__main__":
    sys.exit(main())
