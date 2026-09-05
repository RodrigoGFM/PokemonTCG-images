# Precios de TCGPlayer (actualización diaria)

Este conjunto de archivos agrega, a `PokemonTCG-images` (o al repo que elijas), una actualización
diaria de precios de TCGPlayer tomados de [tcgcsv.com](https://tcgcsv.com), organizados con los
mismos IDs de set que ya usa la app (los de [TCGdex](https://tcgdex.dev)).

## Por qué existe esto

TCGdex (la API que usa la app) ya trae precios de TCGplayer en cada carta, pero de forma
incompleta. tcgcsv.com sí tiene casi todos los precios de TCGplayer, pero organizados con sus
propios IDs, sin relación directa con los de TCGdex. Este script hace el trabajo de "traducir"
entre los dos catálogos una vez al día, para que la app solo tenga que leer un JSON simple.

## Cómo instalarlo

1. Copiá estas carpetas/archivos a la raíz del repositorio elegido (podés preguntarme para
   armarte otra versión con las rutas ajustadas si preferís un repo nuevo en vez de reusar
   `PokemonTCG-images`):
   - `.github/workflows/actualizar-precios.yml`
   - `scripts/actualizar_precios.py`
2. En GitHub, andá a **Settings → Actions → General → Workflow permissions** del repo y
   confirmá que esté en **"Read and write permissions"** (el workflow ya pide
   `permissions: contents: write`, pero esa opción del repo tiene que estar habilitada para que
   sea efectivo).
3. Andá a la pestaña **Actions** del repo, elegí "Actualizar precios de TCGPlayer" en la lista de
   la izquierda, y usá **"Run workflow"** para probarlo una vez a mano (no hace falta esperar al
   horario programado).
4. Cuando termine (unos pocos minutos), revisá que se hayan creado/commiteado:
   - `precios/index.json` — resumen general
   - `precios/<idDeSetDeTCGdex>.json` — uno por cada set emparejado
   - `mapeo-sets.json` — la correspondencia entre sets, para poder corregirla a mano

## Qué revisar en la primera corrida

Abrí `precios/index.json` y mirá especialmente:

- `tcgdexSinEmparejar`: sets de TCGdex para los que NO se encontró un set equivalente en
  TCGplayer (puede pasar con sets muy nuevos que TCGplayer todavía no cargó, o nombres raros).
- `tcgcsvSinEmparejar`: grupos de TCGplayer que no se pudieron emparejar con ningún set de
  TCGdex — algunos de estos van a ser productos que NO son sets de cartas (cajas selladas,
  bundles, "Elite Trainer Box" como su propio "grupo", etc.) y está bien que queden afuera.

Para corregir un emparejamiento a mano (o agregar uno que el algoritmo no encontró), editá
`mapeo-sets.json`:

```json
{
  "manual": {
    "sv10": 24123
  },
  "ignorar_group_ids": [24001],
  "automatico": { "...": "..." }
}
```

- `manual`: fuerza que el set de TCGdex `sv10` use el `groupId` `24123` de TCGplayer, sin
  importar lo que haya calculado el algoritmo.
- `ignorar_group_ids`: grupos de TCGplayer que nunca deberían intentar emparejarse con nada (ej.
  un bundle sellado que apareció como su propio "grupo").
- `automatico`: lo arma el script solo, no hace falta tocarlo (pero se puede mirar para
  entender qué emparejó y con qué puntaje de confianza).

## Formato de `precios/<setId>.json`

```json
{
  "tcgdexSetId": "swsh12",
  "tcgplayerGroupId": 23821,
  "actualizado": "2026-08-25T21:35:00Z",
  "cartas": [
    {
      "productId": 517238,
      "numero": "094/195",
      "numeroNormalizado": "94",
      "variantes": {
        "Holofoil": { "low": 1.2, "mid": 2.5, "high": 8.0, "market": 2.1, "directLow": 1.5 },
        "Reverse Holofoil": { "low": 0.8, "mid": 1.4, "high": 4.0, "market": 1.1, "directLow": null }
      }
    }
  ]
}
```

- `productId` es el ID real de TCGPlayer — el mismo valor que a veces ya trae TCGdex en
  `card.pricing.tcgplayer.<variante>.productId`. Esto es lo que le va a permitir a la app cruzar
  por ID directo cuando TCGdex ya lo conoce, y por `numeroNormalizado` (respaldo) cuando TCGdex no
  trae precio para esa carta.
- `numeroNormalizado` quita ceros a la izquierda y el "/total" (ej. "094/195" → "94"), para poder
  compararlo con el `localId` de TCGdex sin preocuparse por el formato exacto.

## Formato de `precios_historial/<setId>.json`

Historial diario de precio por carta, para graficar el precio en el tiempo en el detalle de cada
carta de la app -- se genera y actualiza en la MISMA corrida que arma `precios/<setId>.json`, un
punto por carta por día (nunca más de uno por día, sin importar cuántas veces se dispare el
workflow ese día). Indexado por `numeroNormalizado` (igual que la app ya cruza `precios/<setId>.json`
contra el `localId` de TCGdex), no por `productId`, para que un mismo número de carta acumule un
solo historial aunque tcgcsv le cambie el productId de un año a otro:

```json
{
  "94": [
    { "fecha": "2026-08-25", "precio": 2.1 },
    { "fecha": "2026-08-26", "precio": 2.35 }
  ]
}
```

`precio` es el mismo criterio "representativo" que ya usa la app para ordenar el catálogo por
precio (de cada variante: market, si no mid, si no low, si no high, si no directLow -- y el mayor
entre todas las variantes de la carta), no el precio exacto de una variante puntual. Se recorta a
los últimos 1000 días (`MAX_PUNTOS_HISTORIAL` en el script) para no dejar crecer el archivo para
siempre.

Se guarda SEPARADO de `precios/<setId>.json` a propósito: la app solo necesita bajar este archivo
cuando el usuario realmente abre el gráfico de precio de una carta de ese set (mismo patrón de
descarga perezosa y cache de 24hs que ya usa para el precio del día), no cada vez que pide el
precio actual de una carta.

## Próximo paso

Una vez que corra bien y el `index.json` se vea razonable (la mayoría de los sets emparejados,
pocos "sin emparejar" que valga la pena revisar), seguimos con el lado de la app: agregar la
descarga/cache de estos JSON y la lógica que arma el precio final (tcgcsv primero, TCGdex de
respaldo si un set/carta todavía no está en `precios/`).
