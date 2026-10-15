"""Calendario local del agente y widget mensual de consulta.

Los eventos se guardan únicamente en calendario.json, junto al programa. No se
sincronizan con servicios externos ni necesitan dependencias adicionales.
"""

import calendar
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import date, datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # raíz del proyecto (vive en src/)
ARCHIVO = RAIZ / "calendario.json"
MESES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)
DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


class Calendario:
    """Eventos sencillos en JSON, disponibles entre sesiones y procesos."""

    def __init__(self, archivo: Path | str = ARCHIVO):
        self.archivo = Path(archivo)

    @staticmethod
    def validar_fecha(valor: str) -> str:
        """Comprueba y devuelve una fecha ISO (AAAA-MM-DD)."""
        texto = str(valor).strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", texto):
            raise ValueError("la fecha debe tener formato AAAA-MM-DD")
        try:
            return date.fromisoformat(texto).isoformat()
        except ValueError as error:
            raise ValueError("la fecha debe tener formato AAAA-MM-DD") from error

    def listar(self) -> list[dict]:
        if not self.archivo.exists():
            return []
        datos = json.loads(self.archivo.read_text(encoding="utf-8"))
        eventos = datos.get("eventos", [])
        if not isinstance(eventos, list):
            raise ValueError("el archivo del calendario no contiene una lista de eventos")
        for evento in eventos:
            if not isinstance(evento, dict) or not all(
                isinstance(evento.get(campo), str) for campo in ("id", "titulo", "fecha")
            ):
                raise ValueError("hay un evento con datos incompletos en el calendario")
            self.validar_fecha(evento["fecha"])
        return sorted(eventos, key=lambda e: (e["fecha"], e.get("hora") or "00:00", e["titulo"].lower()))

    def consultar(self, desde: str, hasta: str) -> list[dict]:
        inicio = date.fromisoformat(self.validar_fecha(desde))
        fin = date.fromisoformat(self.validar_fecha(hasta))
        if fin < inicio:
            raise ValueError("la fecha final no puede ser anterior a la inicial")
        return [
            evento for evento in self.listar()
            if inicio <= date.fromisoformat(evento["fecha"]) <= fin
        ]

    def anadir(self, titulo: str, fecha: str, hora: str = "", descripcion: str = "") -> dict:
        titulo = str(titulo).strip()
        if not titulo:
            raise ValueError("el evento necesita un título")
        if len(titulo) > 160:
            raise ValueError("el título no puede superar 160 caracteres")
        if any(ord(caracter) < 32 for caracter in titulo):
            raise ValueError("el título no puede contener caracteres de control")
        fecha = self.validar_fecha(fecha)
        hora = str(hora).strip()
        if hora:
            try:
                if not re.fullmatch(r"\d{1,2}:\d{2}", hora):
                    raise ValueError
                hora = datetime.strptime(hora, "%H:%M").strftime("%H:%M")
            except ValueError as error:
                raise ValueError("la hora debe tener formato HH:MM (24 horas)") from error
        descripcion = str(descripcion).strip()
        if len(descripcion) > 1000:
            raise ValueError("la descripción no puede superar 1000 caracteres")
        if any(ord(caracter) < 32 and caracter not in "\n\t" for caracter in descripcion):
            raise ValueError("la descripción contiene caracteres no válidos")

        evento = {
            "id": uuid.uuid4().hex[:8],
            "titulo": titulo,
            "fecha": fecha,
            "hora": hora,
            "descripcion": descripcion,
        }
        eventos = self.listar()
        eventos.append(evento)
        self._guardar(eventos)
        return evento

    def eliminar(self, identificador: str) -> dict | None:
        identificador = str(identificador).strip()
        eventos = self.listar()
        encontrado = next((e for e in eventos if e["id"] == identificador), None)
        if encontrado is None:
            return None
        self._guardar([e for e in eventos if e["id"] != identificador])
        return encontrado

    def _guardar(self, eventos: list[dict]) -> None:
        self.archivo.parent.mkdir(parents=True, exist_ok=True)
        temporal = self.archivo.with_name(self.archivo.name + ".tmp")
        temporal.write_text(
            json.dumps({"version": 1, "eventos": eventos}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporal.replace(self.archivo)


class WidgetCalendario:
    """Ventana flotante de solo consulta, explorable con ratón."""

    FONDO = "#111820"
    PANEL = "#1a2530"
    TEXTO = "#f1f5f9"
    SUAVE = "#9aa9b8"
    ACENTO = "#8dc8ff"

    def __init__(self, almacen: Calendario | None = None):
        try:
            import tkinter as tk
        except ImportError as error:
            raise RuntimeError(
                "Falta tkinter: en Ubuntu/Debian instala `sudo apt install python3-tk`"
            ) from error

        self.tk = tk
        self.almacen = almacen or Calendario()
        hoy = date.today()
        self.mes = date(hoy.year, hoy.month, 1)
        self.seleccion = hoy
        self.eventos: list[dict] = []
        self.celdas = []
        self._firma_archivo = self._firma()

        self.root = tk.Tk()
        self.root.title("Calendario · Agente")
        self.root.configure(bg=self.FONDO)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        ancho, alto = 390, 500
        x = max(0, (self.root.winfo_screenwidth() - ancho) // 2)
        self.root.geometry(f"{ancho}x{alto}+{x}+90")

        cabecera = tk.Frame(self.root, bg=self.FONDO)
        cabecera.pack(fill="x", padx=14, pady=(12, 8))
        self._boton(cabecera, "‹", self.mes_anterior, ancho=3).pack(side="left")
        self.etiqueta_mes = tk.Label(
            cabecera, text="", bg=self.FONDO, fg=self.TEXTO,
            font=("Segoe UI", 13, "bold"),
        )
        self.etiqueta_mes.pack(side="left", expand=True)
        self._boton(cabecera, "Hoy", self.ir_hoy, ancho=5).pack(side="left", padx=(0, 5))
        self._boton(cabecera, "›", self.mes_siguiente, ancho=3).pack(side="left")

        dias = tk.Frame(self.root, bg=self.FONDO)
        dias.pack(fill="x", padx=10)
        for columna, nombre in enumerate(("L", "M", "X", "J", "V", "S", "D")):
            tk.Label(
                dias, text=nombre, width=5, bg=self.FONDO, fg=self.SUAVE,
                font=("Segoe UI", 9, "bold"),
            ).grid(row=0, column=columna, padx=2, pady=(2, 5))

        self.grilla = tk.Frame(self.root, bg=self.FONDO)
        self.grilla.pack(fill="x", padx=10)
        for columna in range(7):
            self.grilla.grid_columnconfigure(columna, weight=1)
        self._crear_celdas()

        tk.Frame(self.root, bg="#2a3744", height=1).pack(fill="x", padx=14, pady=10)
        self.etiqueta_dia = tk.Label(
            self.root, text="", anchor="w", bg=self.FONDO, fg=self.TEXTO,
            font=("Segoe UI", 10, "bold"),
        )
        self.etiqueta_dia.pack(fill="x", padx=16, pady=(0, 5))

        lista_frame = tk.Frame(self.root, bg=self.FONDO)
        lista_frame.pack(fill="both", expand=True, padx=14)
        self.lista = tk.Listbox(
            lista_frame, height=7, bg=self.PANEL, fg=self.TEXTO,
            selectbackground="#2c465d", relief="flat", borderwidth=0,
            font=("Segoe UI", 9), activestyle="none",
        )
        scroll = tk.Scrollbar(lista_frame, command=self.lista.yview)
        self.lista.configure(yscrollcommand=scroll.set)
        self.lista.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        tk.Label(
            self.root, text="Los eventos se añaden y consultan por voz · guardado local",
            bg=self.FONDO, fg=self.SUAVE, font=("Segoe UI", 8),
        ).pack(pady=(8, 11))

        self.actualizar()
        self.root.after(1500, self._vigilar_cambios)

    def _boton(self, padre, texto, comando, ancho=5):
        return self.tk.Button(
            padre, text=texto, command=comando, width=ancho,
            bg=self.PANEL, fg=self.TEXTO, activebackground="#2c465d",
            activeforeground=self.TEXTO, relief="flat", borderwidth=0,
            font=("Segoe UI", 10, "bold"), cursor="hand2",
        )

    def _crear_celdas(self) -> None:
        for widget in self.grilla.winfo_children():
            widget.destroy()
        self.celdas = []
        for fila in range(6):
            celdas_fila = []
            for columna in range(7):
                boton = self._boton(self.grilla, "", lambda: None, ancho=5)
                boton.grid(row=fila, column=columna, padx=2, pady=2, sticky="nsew", ipady=4)
                celdas_fila.append(boton)
            self.celdas.append(celdas_fila)

    def actualizar(self) -> None:
        try:
            self.eventos = self.almacen.listar()
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            self.eventos = []
        self._pintar_mes()
        self._pintar_dia()

    def _firma(self):
        try:
            stat = self.almacen.archivo.stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    def _vigilar_cambios(self) -> None:
        if not self.root.winfo_exists():
            return
        firma = self._firma()
        if firma != self._firma_archivo:
            self._firma_archivo = firma
            try:
                self.eventos = self.almacen.listar()
                self._pintar_mes()
                self._pintar_dia()
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                pass
        self.root.after(1500, self._vigilar_cambios)

    def _pintar_mes(self) -> None:
        self.etiqueta_mes.config(text=f"{MESES[self.mes.month - 1].capitalize()} {self.mes.year}")
        conteo: dict[str, int] = {}
        for evento in self.eventos:
            conteo[evento["fecha"]] = conteo.get(evento["fecha"], 0) + 1
        semanas = calendar.Calendar(firstweekday=0).monthdayscalendar(self.mes.year, self.mes.month)
        for fila in range(6):
            for columna in range(7):
                dia = semanas[fila][columna] if fila < len(semanas) else 0
                boton = self.celdas[fila][columna]
                if not dia:
                    boton.config(text="", state="disabled", bg=self.FONDO, command=lambda: None)
                    continue
                fecha = date(self.mes.year, self.mes.month, dia)
                cantidad = conteo.get(fecha.isoformat(), 0)
                es_seleccion = fecha == self.seleccion
                es_hoy = fecha == date.today()
                color = "#254866" if es_seleccion else ("#26313b" if es_hoy else self.PANEL)
                marca = "●" if cantidad else " "
                boton.config(
                    text=f"{dia}\n{marca}", bg=color,
                    fg=self.ACENTO if cantidad else self.TEXTO,
                    disabledforeground=self.SUAVE, state="normal",
                    command=lambda fecha=fecha: self.seleccionar(fecha),
                )

    def _pintar_dia(self) -> None:
        fecha = self.seleccion
        self.etiqueta_dia.config(
            text=f"{DIAS[fecha.weekday()].capitalize()} {fecha.day} de "
                 f"{MESES[fecha.month - 1]} de {fecha.year}"
        )
        self.lista.delete(0, "end")
        eventos = [evento for evento in self.eventos if evento["fecha"] == fecha.isoformat()]
        if not eventos:
            self.lista.insert("end", "  Sin eventos guardados")
        for evento in eventos:
            hora = evento.get("hora") or "Todo el día"
            self.lista.insert("end", f"  {hora}  ·  {evento['titulo']}")
            if evento.get("descripcion"):
                self.lista.insert("end", f"      {evento['descripcion']}")

    def seleccionar(self, fecha: date) -> None:
        self.seleccion = fecha
        self._pintar_mes()
        self._pintar_dia()

    def mes_anterior(self) -> None:
        self.mes = date(self.mes.year - (self.mes.month == 1), (self.mes.month - 2) % 12 + 1, 1)
        self.seleccion = self.mes
        self._pintar_mes()
        self._pintar_dia()

    def mes_siguiente(self) -> None:
        self.mes = date(self.mes.year + (self.mes.month == 12), self.mes.month % 12 + 1, 1)
        self.seleccion = self.mes
        self._pintar_mes()
        self._pintar_dia()

    def ir_hoy(self) -> None:
        self.seleccion = date.today()
        self.mes = date(self.seleccion.year, self.seleccion.month, 1)
        self._pintar_mes()
        self._pintar_dia()

    def ejecutar(self) -> None:
        self.root.mainloop()

    def cerrar(self) -> None:
        self.root.destroy()


def abrir_widget() -> subprocess.Popen:
    """Lanza el widget como proceso gráfico independiente del hilo de voz."""
    interprete = Path(sys.executable)
    if os.name == "nt":
        pythonw = interprete.with_name("pythonw.exe")
        if pythonw.exists():
            interprete = pythonw
    opciones = {"cwd": str(RAIZ), "close_fds": True}
    if os.name != "nt":
        opciones["stdout"] = subprocess.DEVNULL
        opciones["stderr"] = subprocess.DEVNULL
    if os.name == "nt":
        opciones["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0)
    return subprocess.Popen([str(interprete), str(Path(__file__).resolve()), "--widget"], **opciones)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--widget":
        WidgetCalendario().ejecutar()
    else:
        print("Abre el calendario desde el agente diciendo: «muéstrame el calendario».")


if __name__ == "__main__":
    main()
