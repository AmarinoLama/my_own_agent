import { CommonModule } from '@angular/common';
import { ChangeDetectorRef, Component, OnDestroy, OnInit, inject } from '@angular/core';
import { FormsModule } from '@angular/forms';

type WantRow = { persona: string; carta: string; cantidad: number; trend: number | null };
type OfferRow = { vendedor: string; carta: string; precio: number | null; cantidad: number; envio: number | null };
type OptimizerResult = {
  optimo_probado: boolean; nodos: number; solicitudes: number; cubiertas: number;
  sin_cubrir: number; trend: number; precio_cartas: number; envio: number;
  total: number; beneficio: number; vendedores: any[]; personas: any[];
  compras: any[]; pendientes: any[]; regla_envio: string;
};

const EXAMPLE_WANTS: WantRow[] = [
  { persona: 'Ana', carta: 'Dragon Shield', cantidad: 1, trend: 10 },
  ...['Ana', 'Luis', 'Marta', 'Pablo', 'Nico'].map(persona => ({ persona, carta: 'Sol Ring', cantidad: 1, trend: 2 })),
];
const EXAMPLE_OFFERS: OfferRow[] = [
  { vendedor: 'Tienda barata', carta: 'Dragon Shield', precio: 6, cantidad: 1, envio: 4 },
  { vendedor: 'Tienda compartida', carta: 'Dragon Shield', precio: 9, cantidad: 1, envio: 1.5 },
  ...['Ana', 'Luis', 'Marta', 'Pablo', 'Nico'].map(() => ({ vendedor: 'Tienda compartida', carta: 'Sol Ring', precio: 1.2, cantidad: 1, envio: 1.5 })),
];

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './app.html',
  styleUrl: './app.css',
})
export class App implements OnInit, OnDestroy {
  private readonly changeDetector = inject(ChangeDetectorRef);
  private importPollId?: number;
  wants: WantRow[] = [];
  offers: OfferRow[] = [];
  result: OptimizerResult | null = null;
  section: 'wants' | 'offers' = 'wants';
  status = 'Conecta una lista de Cardmarket o añade tus wants para empezar.';
  busy = false;
  serverOnline = false;
  showSetup = false;
  private readonly storageKey = 'cardmarket-optimizer-v3';

  ngOnInit(): void {
    this.loadDraft();
    void this.consumeExtensionImport();
    this.importPollId = window.setInterval(() => void this.consumeExtensionImport(), 2000);
    void fetch('/api/health').then(response => {
      this.serverOnline = response.ok;
      if (!response.ok) this.status = 'No responde el servidor del optimizador. Reinícialo con python cardmarket.py.';
      this.changeDetector.markForCheck();
    }).catch(() => {
      this.serverOnline = false;
      this.status = 'No responde el servidor. Comprueba que python cardmarket.py sigue abierto.';
      this.changeDetector.markForCheck();
    });
  }

  ngOnDestroy(): void {
    if (this.importPollId) window.clearInterval(this.importPollId);
  }

  get totalWants(): number { return this.wants.reduce((total, row) => total + (Number(row.cantidad) || 0), 0); }

  private async consumeExtensionImport(): Promise<void> {
    try {
      const response = await fetch('/api/import/next');
      if (!response.ok) return;
      const imported = await response.json();
      if (!imported.wants?.length && !imported.offers?.length) return;
      this.wants = [...this.wants, ...imported.wants.map((row: any) => ({
        persona: String(row.persona ?? imported.person ?? ''), carta: String(row.carta ?? ''),
        cantidad: Number(row.cantidad) || 1,
        trend: row.trend == null ? null : Number(row.trend),
      }))];
      this.offers = [...this.offers, ...imported.offers.map((row: any) => ({
        vendedor: String(row.vendedor ?? ''), carta: String(row.carta ?? ''),
        precio: row.precio == null ? null : Number(row.precio),
        cantidad: Number(row.cantidad) || 1,
        envio: row.envio == null ? null : Number(row.envio),
      }))];
      this.section = imported.wants?.length ? 'wants' : 'offers';
      this.saveDraft();
      this.status = `Importado desde Firefox: ${imported.wants.length} wants y ${imported.offers.length} ofertas. Completa trends/portes marcados pendientes y verifica edición/stock.`;
      this.changeDetector.markForCheck();
    } catch {
      // La versión manual sigue funcionando aunque aún no haya importaciones.
    }
  }
  get totalPeople(): number { return new Set(this.wants.map(row => row.persona.trim().toLocaleLowerCase()).filter(Boolean)).size; }

  addWant(): void { this.wants = [...this.wants, { persona: '', carta: '', cantidad: 1, trend: null }]; this.changed(); }
  addOffer(): void { this.offers = [...this.offers, { vendedor: '', carta: '', precio: null, cantidad: 1, envio: null }]; this.changed(); }
  removeWant(index: number): void { this.wants = this.wants.filter((_, i) => i !== index); this.changed(); }
  removeOffer(index: number): void { this.offers = this.offers.filter((_, i) => i !== index); this.changed(); }

  changed(): void {
    this.result = null;
    this.saveDraft();
  }

  private loadDraft(): void {
    try {
      const saved = localStorage.getItem(this.storageKey);
      if (!saved) return;
      const data = JSON.parse(saved);
      if (Array.isArray(data.wants) && Array.isArray(data.offers)) {
        this.wants = data.wants;
        this.offers = data.offers;
        this.status = 'He recuperado tus datos guardados en este navegador.';
      }
    } catch {
      localStorage.removeItem(this.storageKey);
    }
  }

  private saveDraft(): void {
    localStorage.setItem(this.storageKey, JSON.stringify({ wants: this.wants, offers: this.offers }));
  }

  async optimize(): Promise<void> {
    if (!this.wants.length || !this.offers.length) {
      this.status = 'Añade al menos una want y una oferta antes de optimizar.';
      return;
    }
    const incompleteWant = this.wants.some(row => !row.persona.trim() || !row.carta.trim() || row.trend === null || !Number.isFinite(Number(row.trend)));
    if (incompleteWant) {
      this.section = 'wants';
      this.status = 'Completa persona, carta, cantidad y trend de todas las wants antes de calcular.';
      return;
    }
    const incompleteOffer = this.offers.some(row => !row.vendedor.trim() || !row.carta.trim() || row.precio === null || row.envio === null || !Number.isFinite(Number(row.precio)) || !Number.isFinite(Number(row.envio)));
    if (incompleteOffer) {
      this.section = 'offers';
      this.status = 'Completa vendedor, carta, precio y envío de todas las ofertas. La extensión no adivina trend ni portes.';
      return;
    }
    this.busy = true;
    this.status = 'Calculando la mejor combinación de tiendas y portes…';
    try {
      const response = await fetch('/api/optimize', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ wants: this.wants, offers: this.offers }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || 'El cálculo ha fallado.');
      this.result = payload;
      this.status = `Plan calculado: ${payload.cubiertas} de ${payload.solicitudes} copias cubiertas.`;
      this.changeDetector.markForCheck();
      window.setTimeout(() => document.querySelector('#results')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 50);
    } catch (error) {
      this.status = error instanceof Error ? error.message : 'No se pudo conectar con el optimizador.';
    } finally {
      this.busy = false;
      this.changeDetector.markForCheck();
    }
  }

  async exportExcel(): Promise<void> {
    if (!this.result) return;
    try {
      const response = await fetch('/api/export', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(this.result),
      });
      if (!response.ok) throw new Error('No se pudo crear el Excel.');
      this.download(await response.blob(), 'plan_compra_cardmarket.xlsx');
      this.status = 'Excel descargado con órdenes por vendedor y resumen por persona.';
      this.changeDetector.markForCheck();
    } catch (error) {
      this.status = error instanceof Error ? error.message : 'No se pudo exportar el Excel.';
      this.changeDetector.markForCheck();
    }
  }

  exportJson(): void {
    this.download(new Blob([JSON.stringify({ wants: this.wants, offers: this.offers }, null, 2)], { type: 'application/json' }), 'wants_y_ofertas_cardmarket.json');
  }

  private download(blob: Blob, filename: string): void {
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url; link.download = filename; link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async importJson(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    try {
      const data = JSON.parse(await file.text());
      if (!Array.isArray(data.wants) || !Array.isArray(data.offers)) throw new Error('El archivo debe contener arrays «wants» y «offers».');
        this.wants = data.wants.map((row: any) => ({
        persona: String(row.persona ?? 'Yo'), carta: String(row.carta ?? ''),
        cantidad: Number(row.cantidad) || 1,
        trend: row.trend == null && row.tendencia == null ? null : Number(row.trend ?? row.tendencia),
      }));
      this.offers = data.offers.map((row: any) => ({
        vendedor: String(row.vendedor ?? 'Revisar vendedor'), carta: String(row.carta ?? ''),
        precio: row.precio == null ? null : Number(row.precio), cantidad: Number(row.cantidad) || 1,
        envio: row.envio == null && row['envío'] == null ? null : Number(row.envio ?? row['envío']),
      }));
      this.section = this.wants.length ? 'wants' : 'offers';
      this.changed();
      this.status = 'Importación completada. Comprueba vendedor, precios, trend, edición, cantidad y envío antes de optimizar.';
      this.changeDetector.markForCheck();
    } catch (error) {
      this.status = error instanceof Error ? `No se pudo importar: ${error.message}` : 'Archivo JSON no válido.';
    } finally {
      input.value = '';
      this.changeDetector.markForCheck();
    }
  }

  loadExample(): void {
    this.wants = structuredClone(EXAMPLE_WANTS);
    this.offers = structuredClone(EXAMPLE_OFFERS);
    this.changed();
    this.status = 'Ejemplo de grupo cargado. La tienda compartida reparte el envío entre cinco compradores.';
  }

  clearAll(): void {
    this.wants = [];
    this.offers = [];
    this.result = null;
    this.saveDraft();
    this.status = 'Datos vaciados. Tus próximos cambios se guardarán localmente en este navegador.';
  }

  async copyPlan(): Promise<void> {
    if (!this.result) return;
    const text = this.result.vendedores.map(seller => [
      `VENDEDOR: ${seller.vendedor}`,
      `Compran: ${seller.personas.join(', ')}`,
      `Cartas: ${seller.lista_cartas.map((card: any) => `${card.carta} × ${card.cantidad}`).join(', ')}`,
      `Cartas ${this.money(seller.cartas)} + envío ${this.money(seller.envio)} = ${this.money(seller.total)}`,
    ].join('\n')).join('\n\n');
    try {
      await navigator.clipboard.writeText(text);
      this.status = 'Instrucciones de compra copiadas al portapapeles.';
      this.changeDetector.markForCheck();
    } catch {
      this.status = 'No se pudo copiar. Descarga el Excel para guardar las instrucciones.';
      this.changeDetector.markForCheck();
    }
  }

  money(value: number): string {
    return new Intl.NumberFormat('es-ES', { style: 'currency', currency: 'EUR' }).format(Number(value) || 0);
  }
}
