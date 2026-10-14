"""Thinking orb: el icono del escritorio y la mascota animada del orbe.

    python orbe_art.py                  # regenera iconos y mascota
    python orbe_art.py --solo-icono     # solo assets/orbe.ico y orbe-agente.ico
    python orbe_art.py --solo-mascota   # solo los PNG de assets/mascota

El orbe es una esfera de cristal con núcleo luminoso, remolino interior, halo
exterior y motas en órbita. Cada estado cambia la paleta, el ritmo y los
detalles. Se dibuja con numpy/Pillow a cuatro veces la resolución y se reduce
con LANCZOS, así los bordes quedan suaves en cualquier tamaño.
"""

import argparse
import io
import math
import struct
from pathlib import Path

try:
    import numpy as np
    from PIL import Image
except ImportError:  # sin Pillow/numpy no hay arte que generar
    np = Image = None

RAIZ = Path(__file__).resolve().parent
MASCOTA = RAIZ / "assets" / "mascota"
ICONOS = ((RAIZ / "assets" / "orbe.ico", (127, 212, 255), (18, 70, 168)),
          (RAIZ / "assets" / "orbe-agente.ico", (150, 255, 190), (18, 120, 70)))

SUPERSAMPLE = 4
ANCHO_MASCOTA, ALTO_MASCOTA = 125, 95
TAMANOS_ICONO = (16, 24, 32, 48, 64, 128, 256)

# Cuántos cuadros tiene cada animación (los mismos que tenía la mascota)
CUADROS = {"error": 29, "escucha": 41, "habla": 64, "idle": 41,
           "pausa": 40, "piensa": 33}

# Estado -> (base oscura, color medio, acento del borde, núcleo, halo)
PALETAS = {
    "idle":    ((24, 46, 82), (74, 154, 226), (170, 232, 255), (236, 251, 255), (88, 188, 255)),
    "escucha": ((22, 54, 90), (100, 202, 255), (216, 246, 255), (255, 255, 255), (132, 222, 255)),
    "habla":   ((56, 26, 96), (172, 98, 242), (255, 198, 250), (255, 245, 255), (204, 132, 255)),
    "piensa":  ((38, 34, 100), (122, 118, 250), (198, 192, 255), (250, 248, 255), (142, 132, 255)),
    "error":   ((94, 20, 30), (234, 68, 76), (255, 178, 160), (255, 240, 234), (255, 96, 84)),
    "pausa":   ((34, 44, 60), (98, 118, 150), (158, 178, 204), (208, 222, 238), (112, 136, 172)),
}

# Ritmo de cada estado: vueltas de remolino, respiración, ondas, motas, halo…
RITMOS = {
    "idle":    dict(vueltas=1, respirar=0.030, ondas=0, motas=6, anillo=True, brillo=1.00),
    "escucha": dict(vueltas=1, respirar=0.045, ondas=1, motas=5, anillo=False, brillo=1.12),
    "habla":   dict(vueltas=2, respirar=0.035, ondas=3, motas=6, anillo=False, brillo=1.06),
    "piensa":  dict(vueltas=2, respirar=0.026, ondas=0, motas=8, anillo=True, brillo=1.06),
    "error":   dict(vueltas=1, respirar=0.024, ondas=2, motas=7, anillo=True, brillo=1.00),
    "pausa":   dict(vueltas=1, respirar=0.018, ondas=0, motas=4, anillo=False, brillo=0.52),
}


def _mezcla(a, b, parte):
    """Media entre dos colores RGB."""
    return tuple(int(round(a[i] + (b[i] - a[i]) * parte)) for i in range(3))


def _paleta(estado, color=None, borde=None):
    """Paleta del estado; `color`/`borde` la tiñen para los iconos."""
    base = PALETAS.get(estado, PALETAS["idle"])
    if color is None:
        return base
    oscuro = borde if borde is not None else tuple(int(c * 0.45) for c in color)
    return (tuple(oscuro), tuple(color), _mezcla(color, (255, 255, 255), 0.55),
            _mezcla(color, (255, 255, 255), 0.85), tuple(color))


def _mota(P, A, px, py, sigma, color, fuerza):
    """Añade una mota luminosa (gaussiana) sobre lo ya dibujado."""
    alto, ancho = A.shape[0], A.shape[1]
    radio = max(2, int(3.0 * sigma))
    x0, x1 = int(px) - radio, int(px) + radio + 1
    y0, y1 = int(py) - radio, int(py) + radio + 1
    if x1 <= 0 or y1 <= 0 or x0 >= ancho or y0 >= alto:
        return
    x0, x1 = max(0, x0), min(ancho, x1)
    y0, y1 = max(0, y0), min(alto, y1)
    yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
    a = (np.exp(-((xx - px) ** 2 + (yy - py) ** 2) / (2.0 * sigma * sigma)) * fuerza)[..., None]
    c = np.asarray(color, np.float32)
    P[y0:y1, x0:x1] = c * a + P[y0:y1, x0:x1] * (1.0 - a)
    A[y0:y1, x0:x1] = a + A[y0:y1, x0:x1] * (1.0 - a)


def pintar(estado, fase=0.0, ancho=ANCHO_MASCOTA, alto=ALTO_MASCOTA, nivel=0.0,
           color=None, borde=None, escala=1.0, detalle=True, sup=SUPERSAMPLE):
    """Dibuja un thinking orb del estado dado. `fase` va de 0 a 1 (bucle)."""
    oscuro, medio, acento, nucleo, halo_col = _paleta(estado, color, borde)
    oscuro = np.asarray(oscuro, np.float32)
    medio = np.asarray(medio, np.float32)
    acento = np.asarray(acento, np.float32)
    nucleo = np.asarray(nucleo, np.float32)
    halo_col = np.asarray(halo_col, np.float32)

    ritmo = RITMOS.get(estado, RITMOS["idle"])
    ang = 2.0 * math.pi * (fase % 1.0)
    brillo = ritmo["brillo"]
    if estado == "error":                      # parpadeo del aviso
        brillo *= 0.78 + 0.22 * (0.5 + 0.5 * math.sin(8 * ang))
    esc = escala * (1.0 + ritmo["respirar"] * math.sin(2 * ang))
    if estado == "escucha":
        esc *= 1.0 + 0.08 * min(1.0, max(0.0, nivel))

    W, H = ancho * sup, alto * sup
    cx, cy = W / 2.0, H / 2.0
    R = min(W, H) * 0.335 * esc

    ux = ((np.arange(W, dtype=np.float32) + 0.5 - cx) / R)[None, :]
    uy = ((np.arange(H, dtype=np.float32) + 0.5 - cy) / R)[:, None]
    nx, ny = ux, uy
    r2 = ux * ux + uy * uy
    r = np.sqrt(r2)
    dentro = r2 <= 1.0
    nz = np.sqrt(np.clip(1.0 - r2, 0.0, 1.0))

    luz = np.array([-0.42, -0.58, 0.70], np.float32)
    luz /= np.linalg.norm(luz)
    medio_luz = luz + np.array([0.0, 0.0, 1.0], np.float32)
    medio_luz /= np.linalg.norm(medio_luz)
    rebote = np.array([0.48, 0.66, 0.42], np.float32)
    rebote /= np.linalg.norm(rebote)

    difusa = np.clip((nx * luz[0] + ny * luz[1] + nz * luz[2] + 0.22) / 1.22, 0.0, 1.0)
    fresnel = np.clip(1.0 - nz, 0.0, 1.0) ** 2.4
    especular = np.clip(nx * medio_luz[0] + ny * medio_luz[1] + nz * medio_luz[2], 0.0, 1.0) ** 90
    brillo_ancho = np.clip(nx * medio_luz[0] + ny * medio_luz[1] + nz * medio_luz[2], 0.0, 1.0) ** 9
    luz_rebote = np.clip(nx * rebote[0] + ny * rebote[1] + nz * rebote[2], 0.0, 1.0) ** 3 * 0.5

    # Cuerpo de cristal: base -> color medio según la luz, borde iluminado
    borde_luz = (fresnel ** 2.0) * (0.30 + 0.70 * difusa)
    col = oscuro + (medio - oscuro) * (difusa ** 1.5)[..., None]
    col = col + acento * (borde_luz * 1.10 + luz_rebote * 0.40)[..., None]
    col = col * (1.0 - 0.34 * fresnel * (1.0 - difusa))[..., None]

    # Remolino interior: espiral que gira con la animación, sin tocar el núcleo
    giro = 2.0 * math.pi * ritmo["vueltas"] * (fase % 1.0)
    theta = np.arctan2(ny, nx)
    espiral = 0.5 + 0.5 * np.sin(4.0 * theta - 7.0 * np.sqrt(np.clip(r2, 0.0, None)) + giro)
    malla = 0.5 + 0.5 * np.sin(3.0 * theta + 5.0 * r2 - giro * 0.6)
    banda = np.clip(1.0 - r2, 0.0, 1.0) ** 0.55 * np.clip((r2 - 0.02) / 0.16, 0.0, 1.0)
    nube = (0.25 + 0.75 * (espiral * 0.6 + malla * 0.4)) * banda
    col = col + acento * (nube * 0.46)[..., None]

    # Núcleo luminoso con pulso (y el latido del micrófono si lo hay)
    pulso = 1.0 + 0.28 * math.sin(2 * ang + 0.7) + 0.5 * min(1.0, max(0.0, nivel))
    if estado == "habla":
        pulso += 0.30 * math.sin(6 * ang)
    elif estado == "error":
        pulso *= 0.7 + 0.5 * (0.5 + 0.5 * math.sin(8 * ang))
    elif estado == "pausa":
        pulso *= 0.55
    col = col + nucleo * (np.exp(-r2 / 0.055) * 0.80 * pulso)[..., None]
    col = col + nucleo * (np.exp(-r2 / 0.012) * 0.70 * pulso)[..., None]

    # Ondas que salen del centro (escuchar / hablar / error)
    ondas = int(ritmo["ondas"])
    onda_fuera = np.zeros((H, W), np.float32)
    if ondas:
        avance = (fase * ondas) % 1.0
        distancia = (r - (0.15 + 1.15 * avance)) * R
        ancho_onda = R * 0.05
        onda = np.exp(-(distancia * distancia) / (2.0 * ancho_onda * ancho_onda)) * (1.0 - avance)
        col = col + acento * (onda * 0.42 * dentro)[..., None]
        onda_fuera = onda * (r > 1.0)

    col = col * brillo
    col = col + np.asarray((255.0, 255.0, 255.0), np.float32) * (especular * 1.05 + brillo_ancho * 0.12)[..., None]
    # Por encima de la rodilla los blancos entran suaves en vez de quedarse planos
    rodilla = 195.0
    col = np.where(col < rodilla, col,
                   rodilla + (255.0 - rodilla) * (1.0 - np.exp(-(col - rodilla) / (255.0 - rodilla))))

    a_cuerpo = np.clip((1.0 - r) * R + 0.5, 0.0, 1.0)

    # El brillo se desvanece antes de llegar al borde del lienzo: si no, el
    # halo quedaría recortado en una ventana rectangular visible.
    px_x = np.arange(W, dtype=np.float32) + 0.5
    px_y = np.arange(H, dtype=np.float32) + 0.5
    distancia_borde = np.minimum(np.minimum(px_x, W - px_x)[None, :],
                                 np.minimum(px_y, H - px_y)[:, None])
    ventana = np.clip((distancia_borde - 0.5) / (R * 0.45), 0.0, 1.0)

    # Halo exterior: la luz que se escapa por el cristal
    fuera_px = np.clip(r - 1.0, 0.0, None) * R
    halo = np.exp(-fuera_px / (R * 0.42)) * (0.40 * brillo)
    halo = halo + np.exp(-((r - 1.02) * R / (R * 0.10)) ** 2) * 0.30
    halo = halo + onda_fuera * 0.35
    halo = np.where(r >= 1.0, halo, 0.0)
    halo = np.clip(halo * ventana, 0.0, 0.92)[..., None]

    P = halo_col * halo                      # color premultiplicado
    A = halo.copy()

    # Motas en órbita: las de atrás se pintan antes que el cuerpo
    orbitas = int(ritmo["motas"]) if detalle else min(3, int(ritmo["motas"]))
    inclinacion = -0.38 + 0.5 * math.sin(giro)
    aplastado = 0.44
    cos_i, sin_i = math.cos(inclinacion), math.sin(inclinacion)
    puntos = []
    for i in range(orbitas):
        posicion = 2.0 * math.pi * ((fase * ritmo["vueltas"]) + i / orbitas)
        radio_orb = 1.16 + 0.10 * math.sin(posicion * 2.0 + i)
        u = math.cos(posicion) * radio_orb * R
        v = math.sin(posicion) * radio_orb * R * aplastado
        px = cx + u * cos_i - v * sin_i
        py = cy + u * sin_i + v * cos_i
        delante = math.sin(posicion) >= 0.0
        sigma = (0.045 + 0.022 * (0.5 + 0.5 * math.cos(posicion + i))) * R
        fuerza = (0.35 + 0.45 * (0.5 + 0.5 * math.sin(posicion * 3.0 + i))) * brillo
        fuerza *= min(1.0, max(0.0, min(px, W - px, py, H - py) / (R * 0.35)))
        puntos.append((px, py, sigma, _mezcla(tuple(acento), tuple(nucleo), 0.5), fuerza, delante))

    # Anillo de órbita: la mitad de detrás se pinta con el halo…
    anillo_delante = None
    if ritmo["anillo"] and detalle:
        u = nx * cos_i + ny * sin_i
        v = (-nx * sin_i + ny * cos_i) / aplastado
        d_anillo = (np.sqrt(u * u + v * v) - 1.30) * R
        anillo = np.exp(-(d_anillo * d_anillo) / (2.0 * (R * 0.022) ** 2)) * 0.60 * brillo
        anillo = anillo * ventana
        anillo_atras = np.clip(np.where(v <= 0.0, anillo, 0.0), 0.0, 1.0)[..., None]
        anillo_delante = np.clip(np.where(v > 0.0, anillo, 0.0), 0.0, 1.0)[..., None]
        P = halo_col * anillo_atras + P * (1.0 - anillo_atras)
        A = anillo_atras + A * (1.0 - anillo_atras)

    for px, py, sigma, mota_color, fuerza, delante in puntos:
        if not delante:
            _mota(P, A, px, py, sigma, mota_color, fuerza)

    P = col * a_cuerpo[..., None] + P * (1.0 - a_cuerpo[..., None])
    A = a_cuerpo[..., None] + A * (1.0 - a_cuerpo[..., None])

    for px, py, sigma, mota_color, fuerza, delante in puntos:
        if delante:
            _mota(P, A, px, py, sigma, mota_color, fuerza)

    # …y la de delante cruza por encima del cristal
    if anillo_delante is not None:
        P = halo_col * anillo_delante + P * (1.0 - anillo_delante)
        A = anillo_delante + A * (1.0 - anillo_delante)

    # Color recto para reducir: donde no hay nada dejamos el color del halo,
    # así el LANCZOS no arrastra negro hacia los bordes transparentes.
    alfa = np.clip(A, 0.0, 1.0)
    rgb = np.where(alfa > 1e-3, P / np.maximum(alfa, 1e-3), halo_col)
    rgb = np.clip(rgb, 0.0, 255.0).astype(np.uint8)
    salida = Image.fromarray(np.dstack([rgb, np.clip(alfa * 255.0, 0, 255).astype(np.uint8)]), "RGBA")
    if (ancho, alto) != (W, H):
        salida = salida.resize((ancho, alto), Image.LANCZOS)
        temporales = np.array(salida)
        temporales[..., :3] = np.clip(temporales[..., :3], 0, 255)
        salida = Image.fromarray(temporales, "RGBA")
    return salida


def guardar_mascota(carpeta=MASCOTA, ancho=ANCHO_MASCOTA, alto=ALTO_MASCOTA, verbose=True):
    """Escribe todos los PNG de la mascota (y borra los que sobren)."""
    total_cuadros = 0
    for estado, cuadros in CUADROS.items():
        destino = Path(carpeta) / estado
        destino.mkdir(parents=True, exist_ok=True)
        esperados = set()
        for i in range(cuadros):
            nombre = f"{i:02d}.png"
            esperados.add(nombre)
            pintar(estado, fase=i / cuadros, ancho=ancho, alto=alto).save(destino / nombre)
            total_cuadros += 1
        for viejo in destino.glob("*.png"):
            if viejo.name not in esperados:
                viejo.unlink()
        if verbose:
            print(f"  {estado:<8} {cuadros} cuadros -> {destino}")
    return total_cuadros


def _escribir_ico(destino, imagenes):
    """Guarda un .ico con entradas PNG (Windows Vista o superior)."""
    capas = []
    for _, imagen in imagenes:
        buffer = io.BytesIO()
        imagen.save(buffer, format="PNG")
        capas.append(buffer.getvalue())

    salida = struct.pack("<HHH", 0, 1, len(capas))
    desplazamiento = 6 + 16 * len(capas)
    for (lado, _), png in zip(imagenes, capas):
        salida += struct.pack("<BBBBHHII", lado % 256, lado % 256, 0, 0, 1, 32,
                              len(png), desplazamiento)
        desplazamiento += len(png)
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(salida + b"".join(capas))
    return destino


def escribir_icono(destino, color=(127, 212, 255), borde=(18, 70, 168)):
    """Escribe un .ico con el thinking orb en varios tamaños."""
    imagenes = []
    for lado in TAMANOS_ICONO:
        imagenes.append((lado, pintar("piensa", fase=0.15, ancho=lado, alto=lado,
                                      color=color, borde=borde,
                                      escala=1.0 + (0.14 if lado <= 48 else 0.05),
                                      detalle=lado >= 48)))
    return _escribir_ico(destino, imagenes)


def generar_iconos():
    return [escribir_icono(ruta, color=centro, borde=borde) for ruta, centro, borde in ICONOS]


def main():
    parser = argparse.ArgumentParser(description="Regenera el arte del thinking orb.")
    parser.add_argument("--solo-icono", action="store_true", help="Solo los .ico de assets/")
    parser.add_argument("--solo-mascota", action="store_true", help="Solo los PNG de la mascota")
    args = parser.parse_args()

    if Image is None or np is None:
        parser.error("Hacen falta Pillow y numpy para generar el arte.")

    if not args.solo_mascota:
        print("Iconos:")
        for ruta in generar_iconos():
            print(f"  {ruta}")
    if not args.solo_icono:
        print("Mascota:")
        total = guardar_mascota()
        print(f"  {total} cuadros en {MASCOTA}")


if __name__ == "__main__":
    main()
