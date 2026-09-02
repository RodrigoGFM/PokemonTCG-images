#!/usr/bin/env python3
"""
Descarga diaria de precios de TCGPlayer (vía tcgcsv.com) para el catálogo Pokémon TCG, y los
organiza por set usando los IDs de set que ya usa la app (los de TCGdex, https://api.tcgdex.net).

Por qué existe este script (contexto para quien lo lea después):
- TCGdex ya trae precios de TCGplayer en cada carta, pero de forma incompleta.
- tcgcsv.com sí tiene (casi) todos los precios de TCGplayer, pero organizados con SUS PROPIOS
  IDs de "categoría"/"grupo"/"producto", que no tienen relación directa con los IDs de TCGdex.
- La app YA recibe, cuando TCGdex sí tiene precio para una variante, el productId real de
  TCGPlayer (ver TcgModels.kt -> TcgPrice.productId). Por eso este script NO necesita emparejar
  carta por carta: alcanza con emparejar SETS (tcgcsv "group" <-> TCGdex "set"), y dentro de cada
  set ya emparejado, cruzar productos por NÚMERO de carta como respaldo para las cartas donde
  TCGdex no trae ningún precio (y por lo tanto no trae productId tampoco).

Salida (se commitea a este mismo repo):
  precios/index.json           - resumen: última actualización, cobertura, sets sin emparejar
  precios/<tcgdexSetId>.json   - precios de ESE set (uno por archivo, para que la app solo baje
                                  el set que necesita en cada momento, igual que ya hace con todo
                                  lo demás)
  mapeo-sets.json              - correspondencia TCGdex <-> TCGplayer, con una sección "manual"
                                  editable a mano para corregir o excluir casos puntuales

No requiere secretos ni tokens propios: usa el GITHUB_TOKEN automático que GitHub Actions le da
a cada corrida del workflow para poder hacer commit en este mismo repo.
"""

import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher

import requests

# GitHub Actions (runners "ubuntu-latest") no tiene salida IPv6 confiable — api.tcgdex.net
# publica un registro AAAA además del A, y en algunas corridas el runner intenta conectarse por
# IPv6 primero y falla con "Network is unreachable" (errno 101) antes de probar IPv4 (mismo
# problema documentado en https://github.com/actions/runner-images/issues/668). tcgcsv.com no
# mostró el mismo síntoma en las corridas reales, pero como también publica AAAA, se fuerza IPv4
# para las dos por las dudas — ninguna de las dos necesita IPv6 para funcionar.
import socket
import urllib3.util.connection as _urllib3_conexion

def _forzar_ipv4():
    return socket.AF_INET

_urllib3_conexion.allowed_gai_family = _forzar_ipv4

# --- Configuración ---

TCGCSV_BASE = "https://tcgcsv.com"
# api.tcgdex.net tiene espejos regionales (na2/eu1/eu2) que resuelven a IPs DISTINTAS entre sí
# (confirmado por DNS: na2 apunta a la misma IP que el dominio genérico, eu1 y eu2 son servidores
# realmente distintos). Como la corrida real en GitHub Actions falló dos veces seguidas contra el
# dominio genérico con "Network is unreachable" (ver commit sobre IPv4 forzado, más arriba), se
# prueba una lista de hosts en orden y se seguir con el próximo si uno falla — así, si el problema
# es específico de la ruta de red hacia esa IP en particular (y no algo que el forzado de IPv4 ya
# resuelva del todo), un espejo en otra región puede seguir funcionando igual.
TCGDEX_BASES = [
    "https://api.eu1.tcgdex.net/v2/en",
    "https://api.eu2.tcgdex.net/v2/en",
    "https://api.na2.tcgdex.net/v2/en",
    "https://api.tcgdex.net/v2/en",
]
CATEGORIA_POKEMON_NOMBRE = "pokemon"  # se busca por nombre, no se asume el ID fijo
PAUSA_ENTRE_PEDIDOS = 0.3  # segundos, más conservador que el mínimo sugerido por tcgcsv.com

# Cabecera propia: tcgcsv.com bloquea User-Agents genéricos.
HEADERS = {"User-Agent": "PokedexApp-PriceSync/1.0 (contacto: rgformenti@gmail.com)"}

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR_PRECIOS = os.path.join(RAIZ, "precios")
ARCHIVO_MAPEO = os.path.join(RAIZ, "mapeo-sets.json")

# Umbral mínimo de similitud de nombre para considerar dos sets como "el mismo" (0 a 1).
UMBRAL_SIMILITUD = 0.55


def pedir_json(url, params=None, headers=None):
    """GET con reintentos simples y la pausa de cortesía que pide tcgcsv.com."""
    ultimo_error = None
    for intento in range(3):
        try:
            resp = requests.get(url, params=params, headers=headers or HEADERS, timeout=30)
            resp.raise_for_status()
            time.sleep(PAUSA_ENTRE_PEDIDOS)
            return resp.json()
        except Exception as e:  # noqa: BLE001 - queremos capturar cualquier falla de red/HTTP
            ultimo_error = e
            time.sleep(1.5 * (intento + 1))
    raise RuntimeError(f"No se pudo obtener {url}: {ultimo_error}")


def pedir_json_tcgdex(ruta):
    """Como pedir_json, pero probando cada host de TCGDEX_BASES en orden — si uno falla (ej. un
    espejo regional con problemas de red puntuales), sigue con el próximo en vez de rendirse.
    `ruta` es la parte después de la base, ej. "/sets" o "/sets/base1"."""
    ultimo_error = None
    for base in TCGDEX_BASES:
        try:
            return pedir_json(f"{base}{ruta}")
        except Exception as e:  # noqa: BLE001
            print(f"  Aviso: {base}{ruta} falló ({e}), probando el próximo host de TCGdex...")
            ultimo_error = e
    raise RuntimeError(f"Ningún host de TCGdex respondió para {ruta}: {ultimo_error}")


def normalizar_nombre(texto):
    """Minúsculas, sin acentos, sin puntuación, para comparar nombres de sets entre las dos APIs
    (ej. TCGdex "Scarlet & Violet" vs tcgcsv "SV01: Scarlet & Violet" deben poder emparejarse)."""
    if not texto:
        return ""
    sin_acentos = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    sin_prefijo = re.sub(r"^[a-z0-9]{2,6}\d*\s*:\s*", "", sin_acentos.lower())
    solo_alfanumerico = re.sub(r"[^a-z0-9]+", " ", sin_prefijo)
    return re.sub(r"\s+", " ", solo_alfanumerico).strip()


def normalizar_numero(numero):
    """'094/167' -> '94'; '094' -> '94'; 'SWSH061' se deja tal cual en minúsculas. Los números de
    carta de TCGdex (localId) y de tcgcsv (extendedData "Number") no siempre coinciden en formato
    (ceros a la izquierda, sufijo "/total"), así que los normalizamos antes de comparar."""
    if not numero:
        return ""
    base = str(numero).split("/")[0].strip().lower()
    sin_ceros = base.lstrip("0") or "0"
    return sin_ceros


def similitud(a, b):
    return SequenceMatcher(None, a, b).ratio()


def normalizar_nombre_producto(texto):
    """Minúsculas, sin acentos, sin puntuación — para el nombre de un producto INDIVIDUAL de
    tcgcsv (ej. 'Bulbasaur', 'Pikachu (Blue Border)'), no de un set entero. Se usa solo en
    cruzar_por_nombre; a diferencia de normalizar_nombre (para nombres de SET) no hace falta
    quitar ningún prefijo tipo 'SV01: '."""
    if not texto:
        return ""
    sin_acentos = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    solo_alfanumerico = re.sub(r"[^a-z0-9]+", " ", sin_acentos.lower())
    return re.sub(r"\s+", " ", solo_alfanumerico).strip()


def cruzar_por_nombre(productos, tcgdex_id):
    """Respaldo para sets de tcgcsv cuyos productos NO traen 'Number' en extendedData — ej. "My
    First Battle" (mfb): confirmado consultando tcgcsv.com en vivo que sus 50 productos (thematic
    starter sin numeración propia, a diferencia de una expansión normal) no traen ese campo, así
    que el camino normal de procesar_set los descartaba a TODOS y el set quedaba con "cartas": []
    — la app entonces caía al precio que trae TCGdex por defecto (CardMarket, en euros) para esas
    cartas en vez del de TCGPlayer, que es justo lo que este script existe para arreglar.

    En vez de número, cruzamos cada producto con su carta de TCGdex por NOMBRE (cada producto de
    tcgcsv en este tipo de set tiene un nombre único, ej. distingue "Bulbasaur" de "Bulbasaur (Blue
    Border)"). Devuelve productId -> localId de TCGdex (para que el resto de procesar_set siga
    exactamente el mismo camino que ya usa para el cruce por número — mismo campo "numero" de
    salida, misma normalización). Los productos sin un match único (0 o más de 1 candidato) se
    descartan en silencio, igual que un producto sin match por número en el camino normal.
    """
    cartas = obtener_cartas_tcgdex_de_set(tcgdex_id)
    if not cartas:
        return {}

    nombre_a_local_ids = {}
    for carta in cartas:
        clave = normalizar_nombre_producto(carta.get("name", ""))
        if clave:
            nombre_a_local_ids.setdefault(clave, []).append(carta.get("localId"))

    resultado = {}
    sin_match = []
    for prod in productos:
        clave = normalizar_nombre_producto(prod.get("name", ""))
        candidatos = nombre_a_local_ids.get(clave)
        if candidatos and len(candidatos) == 1:
            resultado[prod["productId"]] = candidatos[0]
        else:
            sin_match.append(prod.get("name"))

    if sin_match:
        print(f"  {tcgdex_id}: cruce por nombre sin match para {len(sin_match)} producto(s): "
              f"{', '.join(str(n) for n in sin_match[:10])}"
              f"{' ...' if len(sin_match) > 10 else ''}")

    return resultado


# --- Paso 1: traer categorías, grupos (tcgcsv) y sets (TCGdex) ---

def obtener_categoria_pokemon():
    data = pedir_json(f"{TCGCSV_BASE}/tcgplayer/categories")
    for categoria in data.get("results", []):
        if CATEGORIA_POKEMON_NOMBRE in categoria.get("name", "").lower():
            return categoria["categoryId"]
    raise RuntimeError("No se encontró la categoría 'Pokemon' en tcgcsv.com/tcgplayer/categories")


def obtener_grupos_tcgcsv(category_id):
    data = pedir_json(f"{TCGCSV_BASE}/tcgplayer/{category_id}/groups")
    return data.get("results", [])


def obtener_sets_tcgdex():
    # Set "brief": id, name, releaseDate, symbol, logo, cardCount — ver TcgSetInfo en TcgModels.kt
    return pedir_json_tcgdex("/sets")


def obtener_cartas_tcgdex_de_set(tcgdex_id):
    """Detalle completo de UN set de TCGdex (incluye su arreglo de cartas 'brief': id/localId/
    name/image). Solo se pide para sets que lo necesitan (ver cruzar_por_nombre) — no para los
    ~200 sets normales, que ya se cruzan por número sin tener que bajar sus cartas."""
    try:
        detalle = pedir_json_tcgdex(f"/sets/{tcgdex_id}")
    except Exception as e:  # noqa: BLE001
        print(f"  ERROR al pedir el detalle de {tcgdex_id} para cruzar por nombre: {e}")
        return []
    return detalle.get("cards", []) or []


# --- Paso 2: emparejar sets de tcgcsv con sets de TCGdex ---

def calcular_candidatos(grupos_tcgcsv, sets_tcgdex):
    """Para cada grupo de tcgcsv, calcula un puntaje de similitud contra cada set de TCGdex.
    Devuelve una lista de (puntaje, groupId, tcgdexId) ordenada de mayor a menor puntaje, para
    poder armar un emparejamiento 1 a 1 de forma golosa (greedy) más abajo."""
    nombres_tcgdex = {s["id"]: normalizar_nombre(s.get("name", "")) for s in sets_tcgdex}

    candidatos = []
    for grupo in grupos_tcgcsv:
        nombre_grupo = normalizar_nombre(grupo.get("name", ""))
        if not nombre_grupo:
            continue
        for tcgdex_id, nombre_tcgdex in nombres_tcgdex.items():
            if not nombre_tcgdex:
                continue
            puntaje = similitud(nombre_grupo, nombre_tcgdex)
            # Corrección importante (encontrada revisando la primera corrida real): cuando el
            # nombre de TCGdex es corto y "genérico" (ej. "Scarlet & Violet", "XY", "Sun & Moon" —
            # el set BASE de una generación) y el de tcgcsv es "<nombre> Base Set", el ratio de
            # SequenceMatcher penaliza más ese sufijo largo que un sufijo corto tipo "151" o
            # "Promos" — así que, por pura aritmética de longitud, terminaba prefiriendo el set
            # EQUIVOCADO (ej. "SV: Scarlet & Violet 151" en vez de "SV01: ... Base Set"). Como
            # "Base Set" es exactamente la forma en que TCGplayer nombra al set base de cada
            # generación, si el nombre de tcgcsv termina en "base set" y el de TCGdex no contiene
            # ya las palabras "base"/"set" (para no afectar sets que sí se llaman así en TCGdex),
            # empujamos el puntaje hacia arriba en vez de dejar que la longitud del sufijo decida.
            if nombre_grupo.endswith("base set") and "base" not in nombre_tcgdex and "set" not in nombre_tcgdex:
                puntaje = max(puntaje, similitud(nombre_grupo.removesuffix(" base set"), nombre_tcgdex) + 0.05)
            puntaje = min(puntaje, 1.0)  # el bonus de arriba puede pasarse de 1.0, es solo un desempate
            if puntaje >= UMBRAL_SIMILITUD:
                candidatos.append((puntaje, grupo["groupId"], tcgdex_id))

    candidatos.sort(key=lambda x: x[0], reverse=True)
    return candidatos


def emparejar_greedy(candidatos):
    """Asignación 1 a 1: recorre los candidatos de mayor a menor puntaje y va asignando mientras
    ninguno de los dos lados ya esté usado. Simple pero suficiente para ~200 sets por año."""
    grupos_usados = set()
    tcgdex_usados = set()
    mapeo = {}
    for puntaje, group_id, tcgdex_id in candidatos:
        if group_id in grupos_usados or tcgdex_id in tcgdex_usados:
            continue
        mapeo[tcgdex_id] = {"groupId": group_id, "puntaje": round(puntaje, 3)}
        grupos_usados.add(group_id)
        tcgdex_usados.add(tcgdex_id)
    return mapeo


def cargar_mapeo_existente():
    if os.path.exists(ARCHIVO_MAPEO):
        with open(ARCHIVO_MAPEO, "r", encoding="utf-8") as f:
            datos = json.load(f)
            datos.setdefault("catalogo_tcgcsv", {})
            datos.setdefault("ignorar_tcgdex_ids", [])
            datos.setdefault("gruposAdicionales", {})
            return datos
    return {
        "manual": {}, "ignorar_group_ids": [], "automatico": {}, "catalogo_tcgcsv": {},
        "ignorar_tcgdex_ids": [], "gruposAdicionales": {},
    }


def construir_mapeo(grupos_tcgcsv, sets_tcgdex):
    mapeo_guardado = cargar_mapeo_existente()
    manual = mapeo_guardado.get("manual", {})  # tcgdexId -> groupId, a mano, tiene prioridad
    ignorar = set(mapeo_guardado.get("ignorar_group_ids", []))  # groupIds que NO son sets reales
    # (ej. cajas selladas, bundles) — se agregan a mano después de revisar "sin_emparejar"
    # OJO: los grupos ya asignados a mano NO se excluyen acá del pool automático — un mismo
    # groupId de tcgcsv puede ser, legítimamente, el precio correcto para MÁS DE UN set de
    # TCGdex a la vez (ej. "exu" está mapeado a mano al mismo grupo que "ex10", porque de verdad
    # comparten el mismo producto físico en TCGplayer — "ex10" todavía necesita emparejarse
    # automáticamente con ESE grupo). Se probó excluir estos grupos y rompió justo ese caso. El
    # problema real que motivó esa idea (un set de PRUEBA de TCGdex, "sp"/"Sample", que no es una
    # expansión real, se emparejaba por casualidad de nombre con un grupo ya usado por "sm1") se
    # resuelve mejor excluyendo ESE set puntual de TCGdex (ver "ignorar_tcgdex_ids" abajo) en vez
    # de bloquear el grupo para todo el mundo.
    ids_tcgdex_ignorados = set(mapeo_guardado.get("ignorar_tcgdex_ids", []))
    sets_tcgdex = [s for s in sets_tcgdex if s["id"] not in ids_tcgdex_ignorados]
    # Los grupos que se cubren como "catálogo completo" (ver catalogo_tcgcsv más abajo, y
    # procesar_set_catalogo_completo) tampoco son candidatos para el emparejamiento normal —
    # ya tienen su propio archivo de salida armado directamente desde tcgcsv, y no representan
    # ningún set real de TCGdex con el que puedan compartirse legítimamente.
    catalogo_tcgcsv = mapeo_guardado.get("catalogo_tcgcsv", {})  # setIdSintetico -> groupId
    ignorar = ignorar | set(catalogo_tcgcsv.values())

    grupos_validos = [g for g in grupos_tcgcsv if g["groupId"] not in ignorar]
    candidatos = calcular_candidatos(grupos_validos, sets_tcgdex)
    automatico = emparejar_greedy(candidatos)

    # El mapeo manual pisa cualquier resultado automático para ese tcgdexId.
    mapeo_final = dict(automatico)
    ids_grupo_por_manual = set()
    for tcgdex_id, group_id in manual.items():
        mapeo_final[tcgdex_id] = {"groupId": group_id, "puntaje": None, "manual": True}
        ids_grupo_por_manual.add(group_id)

    ids_tcgdex_todos = {s["id"] for s in sets_tcgdex}
    ids_grupo_todos = {g["groupId"] for g in grupos_validos}
    tcgdex_sin_emparejar = sorted(ids_tcgdex_todos - set(mapeo_final.keys()))
    grupos_usados = {v["groupId"] for v in mapeo_final.values()} | ids_grupo_por_manual
    grupos_sin_emparejar = sorted(ids_grupo_todos - grupos_usados)

    # Persistimos el resultado automático (no el final con manual mezclado) para que la próxima
    # corrida no tenga que recalcular todo, y para que sea fácil ver qué decidió el algoritmo vs.
    # qué corrigió una persona.
    mapeo_guardado["automatico"] = {k: v for k, v in automatico.items() if k not in manual}
    guardar_mapeo(mapeo_guardado)

    grupos_adicionales = mapeo_guardado.get("gruposAdicionales", {})
    return mapeo_final, tcgdex_sin_emparejar, grupos_sin_emparejar, catalogo_tcgcsv, grupos_adicionales


def guardar_mapeo(mapeo_guardado):
    with open(ARCHIVO_MAPEO, "w", encoding="utf-8") as f:
        json.dump(mapeo_guardado, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


# --- Paso 3: por cada set emparejado, traer productos + precios y armar el JSON de salida ---

def procesar_set(tcgdex_id, group_id, grupos_adicionales=None):
    """`grupos_adicionales`: IDs de grupo de tcgcsv EXTRA para fusionar en el mismo archivo de
    salida, ademas de `group_id` -- caso real: TCGplayer separa "Generations" (g1, groupId 1728)
    de su subset "Generations: Radiant Collection" (groupId 1729) en dos grupos DISTINTOS, pero
    TCGdex trata todo como un solo set g1 (localIds "RC1".."RC32" para el subset) -- sin esto,
    esas 32 cartas nunca tienen precio porque su groupId real jamas se consulta."""
    grupos = [group_id] + list(grupos_adicionales or [])
    productos = []
    precios = []
    for gid in grupos:
        productos.extend(pedir_json(f"{TCGCSV_BASE}/tcgplayer/3/{gid}/products").get("results", []))
        precios.extend(pedir_json(f"{TCGCSV_BASE}/tcgplayer/3/{gid}/prices").get("results", []))

    precios_por_producto = {}
    for p in precios:
        precios_por_producto.setdefault(p["productId"], []).append(p)

    numero_por_producto = {}
    nombre_por_producto = {prod["productId"]: prod.get("name") for prod in productos}
    for prod in productos:
        numero = None
        for campo in prod.get("extendedData", []) or []:
            if campo.get("name") == "Number":
                numero = campo.get("value")
                break
        if numero:
            numero_por_producto[prod["productId"]] = numero

    # Respaldo: si NINGÚN producto de este set trae número de carta (ej. "My First Battle" — ver
    # cruzar_por_nombre), tcgcsv simplemente no numera esta colección. En vez de descartar el set
    # entero, cruzamos por nombre contra TCGdex. Se activa solo cuando el camino normal no
    # encontró NINGÚN número en absoluto — nunca para un set que numera bien pero le falta en 1 o
    # 2 productos sueltos (esos siguen descartándose como "no es una carta individual", como
    # siempre) — para no arriesgar falsos positivos en los ~200 sets que ya funcionan bien.
    if productos and not numero_por_producto:
        numero_por_producto = cruzar_por_nombre(productos, tcgdex_id)

    # Cuántos productos comparten el mismo número normalizado dentro de este set — en sets viejos
    # (Base Set, Jungle, Fossil, etc.) una misma carta puede tener varios PRODUCTOS separados en
    # TCGplayer (ej. Unlimited / Shadowless / 1st Edition de un mismo Charizard), cada uno con su
    # propio productId, en vez de ser sub-precios de un solo producto. Ver "precios-tcgplayer.md"
    # -> sección de variantes viejas para el detalle completo de por qué hace falta esto.
    conteo_por_numero = {}
    for product_id, numero in numero_por_producto.items():
        clave = normalizar_numero(numero)
        conteo_por_numero[clave] = conteo_por_numero.get(clave, 0) + 1

    entradas = []
    for product_id, filas_precio in precios_por_producto.items():
        numero = numero_por_producto.get(product_id)
        if not numero:
            continue  # No es una carta individual (ej. un booster box, un bundle) — no aplica
        variantes = {}
        for fila in filas_precio:
            variante = fila.get("subTypeName") or "Normal"
            variantes[variante] = {
                "low": fila.get("lowPrice"),
                "mid": fila.get("midPrice"),
                "high": fila.get("highPrice"),
                "market": fila.get("marketPrice"),
                "directLow": fila.get("directLowPrice"),
            }
        entrada = {
            "productId": product_id,
            "numero": numero,
            "numeroNormalizado": normalizar_numero(numero),
            "variantes": variantes,
        }
        # Solo se manda el nombre real del producto de tcgcsv cuando hace falta para desambiguar
        # (más de un producto comparte este número) — para los ~200 sets normales sin este
        # problema, no se repite ningún dato que la app ya tenga por TCGdex.
        if conteo_por_numero.get(normalizar_numero(numero), 0) > 1:
            entrada["nombreProducto"] = nombre_por_producto.get(product_id)
        entradas.append(entrada)

    salida = {
        "tcgdexSetId": tcgdex_id,
        "tcgplayerGroupId": group_id,
        "actualizado": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cartas": entradas,
    }
    if grupos_adicionales:
        salida["tcgplayerGruposAdicionales"] = list(grupos_adicionales)
    return salida


def procesar_set_catalogo_completo(set_id, group_id, nombre_set):
    """Para sets que NO existen en TCGdex (ej. Pikachu World Collection Promos): a diferencia de
    procesar_set, que solo agrega precios sobre cartas que la app ya conoce por TCGdex, acá no hay
    ningún catálogo previo — armamos la carta completa (nombre, número, imagen, rareza, tipo, y
    cualquier otro atributo que traiga tcgcsv) directamente desde tcgcsv, además del precio. La
    app necesita tratar estos archivos distinto de los demás (ver "esCatalogoCompleto" en el
    JSON): no hay que cruzarlos con TCGdex, hay que mostrarlos como su propio set independiente.
    """
    productos = pedir_json(f"{TCGCSV_BASE}/tcgplayer/3/{group_id}/products").get("results", [])
    precios = pedir_json(f"{TCGCSV_BASE}/tcgplayer/3/{group_id}/prices").get("results", [])

    precios_por_producto = {}
    for p in precios:
        precios_por_producto.setdefault(p["productId"], []).append(p)

    entradas = []
    for prod in productos:
        atributos = {}
        numero = None
        for campo in prod.get("extendedData", []) or []:
            nombre_campo = campo.get("name")
            valor_campo = campo.get("value")
            if nombre_campo == "Number":
                numero = valor_campo
            elif valor_campo:
                atributos[nombre_campo] = valor_campo
        if not numero:
            continue  # No es una carta individual (ej. una caja, un bundle) — no aplica

        variantes = {}
        for fila in precios_por_producto.get(prod["productId"], []):
            variante = fila.get("subTypeName") or "Normal"
            variantes[variante] = {
                "low": fila.get("lowPrice"),
                "mid": fila.get("midPrice"),
                "high": fila.get("highPrice"),
                "market": fila.get("marketPrice"),
                "directLow": fila.get("directLowPrice"),
            }

        entradas.append({
            "productId": prod["productId"],
            "nombre": prod.get("name"),
            "numero": numero,
            "numeroNormalizado": normalizar_numero(numero),
            # tcgcsv solo da esta miniatura (200px de ancho) — si hace falta una imagen más
            # grande para el detalle de la carta, queda pendiente investigar si TCGplayer expone
            # otro tamaño en una URL parecida.
            "imagen": prod.get("imageUrl"),
            "rareza": atributos.pop("Rarity", None),
            "tipo": atributos.pop("Card Type", None),
            "atributos": atributos,  # el resto (HP, ataques, debilidad, etc.), tal cual lo da tcgcsv
            "variantes": variantes,
        })

    entradas.sort(key=lambda c: c["numeroNormalizado"])

    return {
        "tcgdexSetId": set_id,
        "esCatalogoCompleto": True,
        "nombre": nombre_set,
        "tcgplayerGroupId": group_id,
        "actualizado": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cartas": entradas,
    }


def main():
    os.makedirs(DIR_PRECIOS, exist_ok=True)

    print("Buscando categoría Pokémon en tcgcsv.com...")
    category_id = obtener_categoria_pokemon()

    print("Descargando grupos (sets) de tcgcsv.com...")
    grupos = obtener_grupos_tcgcsv(category_id)
    print(f"  {len(grupos)} grupos encontrados.")

    print("Descargando sets de TCGdex...")
    sets_tcgdex = obtener_sets_tcgdex()
    print(f"  {len(sets_tcgdex)} sets encontrados.")

    print("Emparejando sets...")
    mapeo, tcgdex_sin_emparejar, grupos_sin_emparejar, catalogo_tcgcsv, grupos_adicionales = construir_mapeo(
        grupos, sets_tcgdex
    )
    print(f"  {len(mapeo)} sets emparejados, {len(tcgdex_sin_emparejar)} sets de TCGdex sin "
          f"emparejar, {len(grupos_sin_emparejar)} grupos de tcgcsv sin emparejar.")

    nombres_grupo_por_id = {g["groupId"]: g["name"] for g in grupos}
    nombres_tcgdex_por_id = {s["id"]: s["name"] for s in sets_tcgdex}

    resumen_sets = []
    total_cartas_con_precio = 0
    print(f"Descargando precios de {len(mapeo)} sets emparejados...")
    for i, (tcgdex_id, info) in enumerate(sorted(mapeo.items()), start=1):
        group_id = info["groupId"]
        extra = grupos_adicionales.get(tcgdex_id, [])
        try:
            salida = procesar_set(tcgdex_id, group_id, extra)
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}/{len(mapeo)}] ERROR en {tcgdex_id} (group {group_id}): {e}")
            continue
        with open(os.path.join(DIR_PRECIOS, f"{tcgdex_id}.json"), "w", encoding="utf-8") as f:
            json.dump(salida, f, ensure_ascii=False, indent=2)
            f.write("\n")
        total_cartas_con_precio += len(salida["cartas"])
        resumen_sets.append({
            "tcgdexSetId": tcgdex_id,
            "tcgdexNombre": nombres_tcgdex_por_id.get(tcgdex_id),
            "tcgplayerGroupId": group_id,
            "tcgplayerNombre": nombres_grupo_por_id.get(group_id),
            "cartas": len(salida["cartas"]),
            "puntajeEmparejamiento": info.get("puntaje"),
        })
        if i % 20 == 0:
            print(f"  [{i}/{len(mapeo)}] procesados...")

    # Lista corta de sets emparejados con poca confianza (puntaje bajo, o 0 cartas encontradas a
    # pesar de estar "emparejado") — para no tener que releer los ~200 sets enteros en cada
    # revisión. Los emparejamientos manuales (mapeo-sets.json -> "manual") no entran acá: ya
    # fueron confirmados a mano, así que no hace falta volver a dudar de ellos cada corrida.
    UMBRAL_CONFIANZA = 0.75
    baja_confianza = [
        s for s in resumen_sets
        if not (mapeo.get(s["tcgdexSetId"]) or {}).get("manual")
        and (
            (s["puntajeEmparejamiento"] is not None and s["puntajeEmparejamiento"] < UMBRAL_CONFIANZA)
            or s["cartas"] == 0
        )
    ]

    # Sets que no existen en TCGdex y se arman enteros (catálogo + precio) directamente desde
    # tcgcsv — ver procesar_set_catalogo_completo y mapeo-sets.json -> "catalogo_tcgcsv".
    catalogos_completos = []
    if catalogo_tcgcsv:
        print(f"Armando {len(catalogo_tcgcsv)} set(s) sin TCGdex desde tcgcsv...")
    for set_id, group_id in sorted(catalogo_tcgcsv.items()):
        nombre_set = nombres_grupo_por_id.get(group_id, set_id)
        try:
            salida = procesar_set_catalogo_completo(set_id, group_id, nombre_set)
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR en {set_id} (group {group_id}): {e}")
            continue
        with open(os.path.join(DIR_PRECIOS, f"{set_id}.json"), "w", encoding="utf-8") as f:
            json.dump(salida, f, ensure_ascii=False, indent=2)
            f.write("\n")
        total_cartas_con_precio += len(salida["cartas"])
        catalogos_completos.append({
            "tcgdexSetId": set_id,
            "nombre": nombre_set,
            "tcgplayerGroupId": group_id,
            "cartas": len(salida["cartas"]),
        })

    indice = {
        "actualizado": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "setsCubiertos": len(resumen_sets),
        "cartasConPrecio": total_cartas_con_precio,
        "sets": resumen_sets,
        "bajaConfianza": baja_confianza,
        "tcgdexSinEmparejar": [
            {"id": tid, "nombre": nombres_tcgdex_por_id.get(tid)} for tid in tcgdex_sin_emparejar
        ],
        "tcgcsvSinEmparejar": [
            {"groupId": gid, "nombre": nombres_grupo_por_id.get(gid)} for gid in grupos_sin_emparejar
        ],
        # Sets sin TCGdex, armados enteros desde tcgcsv (ver "esCatalogoCompleto" en su propio
        # precios/<id>.json) — la app los tiene que tratar distinto: no hay set de TCGdex al que
        # pegarles precios, HAY que mostrar la carta entera (nombre, imagen, etc.) desde ese JSON.
        "catalogosCompletosTcgcsv": catalogos_completos,
    }
    with open(os.path.join(DIR_PRECIOS, "index.json"), "w", encoding="utf-8") as f:
        json.dump(indice, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"Listo. {len(resumen_sets)} sets con precios, {total_cartas_con_precio} cartas en total.")
    print(f"  {len(baja_confianza)} emparejamientos de baja confianza (ver precios/index.json -> bajaConfianza).")
    print(f"Revisar precios/index.json -> tcgdexSinEmparejar / tcgcsvSinEmparejar para casos a mano.")


if __name__ == "__main__":
    sys.exit(main())
