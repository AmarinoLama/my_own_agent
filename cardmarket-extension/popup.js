const api = typeof browser !== 'undefined' ? browser : chrome;
const $ = id => document.getElementById(id);
let activeTabId = null;
let detected = null;

function setStatus(message, error = false) {
  $('status').textContent = message;
  $('status').classList.toggle('error', error);
}

function scanPage() {
  setStatus('Leyendo únicamente el contenido visible de esta pestaña…');
  $('import').disabled = true;
  api.tabs.query({ active: true, currentWindow: true }).then(tabs => {
    const tab = tabs[0];
    activeTabId = tab?.id ?? null;
    if (!tab?.url || !/^https?:\/\/(?:www\.)?cardmarket\.com(?:\/|$)/i.test(tab.url)) {
      detected = null;
      $('list').innerHTML = '<option value="">Abre primero una página de Cardmarket</option>';
      $('preview').hidden = true;
      setStatus('Esta pestaña no es Cardmarket. Abre una página o want-list y vuelve a detectarla.', true);
      return;
    }
    api.tabs.executeScript(tab.id, { code: `(${readVisibleCardmarket.toString()})()` }).then(results => {
      detected = results?.[0] || null;
      showDetected(detected);
    }).catch(() => setStatus('Firefox no pudo leer esta página. Recarga Cardmarket, vuelve a aceptar cookies si aparece el diálogo y pulsa detectar.', true));
  }).catch(() => setStatus('No se pudo consultar la pestaña activa.', true));
}

function readVisibleCardmarket() {
  const visible = element => {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  };
  const clean = text => (text || '').replace(/\s+/g, ' ').trim();
  const cardLink = row => [...row.querySelectorAll('a[href]')].find(link => {
    const href = link.href;
    return /cardmarket\./i.test(href) && /\/Products?\//i.test(new URL(href).pathname) && clean(link.innerText).length > 1;
  });
  const extractQuantity = row => {
    const inputs = [...row.querySelectorAll('input,select')].filter(visible);      const input = inputs.find(el => /quantity|amount|count|cantidad|want|qty/i.test([el.name, el.id, el.getAttribute('aria-label'), el.placeholder, el.title].join(' ')))
      || inputs.find(el => /number/i.test(el.type || '') || el.tagName === 'SELECT');

    const value = Number(input?.value);
    if (Number.isInteger(value) && value > 0 && value <= 200) return value;
    const text = clean(row.innerText);
    const patterns = [/(?:quantity|amount|cantidad|qty|wants?)\s*[:x×]?\s*(\d{1,3})/i, /(?:^|\s)[×x]\s*(\d{1,3})(?:\s|$)/];
    for (const pattern of patterns) {
      const match = text.match(pattern);
      if (match && Number(match[1]) > 0 && Number(match[1]) <= 200) return Number(match[1]);
    }
    // A wants table usually puts the requested number in a dedicated numeric cell.
    const cells = [...row.querySelectorAll('td')].filter(visible);
    for (const cell of cells.slice(-3)) {
      const textValue = clean(cell.innerText);
      if (/^\d{1,3}$/.test(textValue) && Number(textValue) > 0 && Number(textValue) <= 200) return Number(textValue);
    }
    return 1;
  };
  const extractPrice = row => {
    const priceElement = row.querySelector('[class*="price"], [data-testid*="price"], [itemprop="price"]');
    const candidates = [priceElement?.getAttribute('content'), priceElement?.innerText, row.innerText].filter(Boolean);
    for (const candidate of candidates) {
      const match = String(candidate).match(/(?:€\s*(\d{1,5}(?:[.,]\d{1,2})?)|(\d{1,5}(?:[.,]\d{1,2})?)\s*€)/);
      if (match) return Number((match[1] || match[2]).replace(',', '.'));
    }
    return null;
  };
  const productRows = [...document.querySelectorAll('tr, [role="row"], .article-row, .product-row')].filter(visible);
  const rows = [];
  const seen = new Set();
  for (const row of productRows) {
    const link = cardLink(row);
    if (!link) continue;
    const href = new URL(link.href);
    const key = `${href.pathname}|${clean(link.innerText).toLocaleLowerCase()}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const sellerAnchor = [...row.querySelectorAll('a[href]')].find(a => /\/Users?\//i.test(new URL(a.href).pathname) && clean(a.innerText));
    const price = extractPrice(row);
    const name = clean(link.innerText).replace(/\s+/g, ' ');
    const visibleRowText = clean(row.innerText);
    // On wants-list tables the quantity is usually in a numeric cell, while the
    // same product anchor may repeat in nested/responsive duplicate table nodes.
    rows.push({
      carta: name,
      cantidad: extractQuantity(row),
      vendedor: sellerAnchor ? clean(sellerAnchor.innerText) : '',
      precio: price,
      url: href.href,
      text: visibleRowText.slice(0, 500),
    });
  }
  const pageTitle = clean(document.title);
  const headings = [...document.querySelectorAll('h1,h2,main h3')].filter(visible).map(el => clean(el.innerText)).filter(Boolean);
  const listTitle = headings[0] || pageTitle;
  const uniqueRows = rows.filter((row, index) => rows.findIndex(candidate =>
    candidate.url === row.url && candidate.carta.toLocaleLowerCase() === row.carta.toLocaleLowerCase()
  ) === index);
  return {
    pageTitle,
    listTitle,
    url: location.href,
    rows: uniqueRows,
    kind: uniqueRows.some(row => row.vendedor && row.precio !== null) ? 'offers' : 'wants',
  };
}

function showDetected(data) {
  if (!data?.rows?.length) {
    $('list').innerHTML = '<option value="">No se detectaron filas de cartas</option>';
    $('preview').hidden = true;
    setStatus('No he detectado cartas en esta vista. Abre la tabla de wants u ofertas; no leo datos ocultos ni cargo más páginas automáticamente.', true);
    return;
  }
  const wants = data.rows.filter(row => row.carta);
  $('list').innerHTML = `<option value="0">${escapeHtml(data.listTitle)} · ${wants.length} carta(s)</option>`;
  $('includeOffers').checked = data.kind === 'offers';
  $('includeWants').checked = data.kind !== 'offers';
  $('preview').hidden = false;
  $('preview').innerHTML = `<table><thead><tr><th>Carta visible</th><th>Copias</th><th>Vendedor / precio visto</th></tr></thead><tbody>${wants.slice(0, 6).map(row => `<tr><td>${escapeHtml(row.carta)}</td><td>${row.cantidad}</td><td>${escapeHtml(row.vendedor ? `${row.vendedor} · ${row.precio ?? 'precio n/d'}` : 'want')}</td></tr>`).join('')}${wants.length > 6 ? `<tr><td colspan="3">… y ${wants.length - 6} carta(s) más</td></tr>` : ''}</tbody></table>`;
  $('import').disabled = false;
  setStatus(`Vista detectada: ${wants.length} fila(s). Revisa la previsualización; algunos diseños pueden requerir copiar la lista manualmente.`);
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

$('scan').addEventListener('click', scanPage);
$('openApp').addEventListener('click', () => api.tabs.create({ url: 'http://127.0.0.1:8765/' }));
$('import').addEventListener('click', async () => {
  if (!detected || !activeTabId) return;
  const person = $('person').value.trim();
  if ($('includeWants').checked && !person) {
    $('person').focus();
    setStatus('Escribe quién es el dueño de esta lista antes de importarla.', true);
    return;
  }
  const wants = $('includeWants').checked ? detected.rows.map(row => ({
    persona: person,
    carta: row.carta,
    cantidad: row.cantidad || 1,
    // Trend is deliberately not guessed; confirm or enter the reference price in Cardpilot.
    trend: null,
    sourceUrl: row.url,
  })) : [];
  const offers = $('includeOffers').checked ? detected.rows.filter(row => row.vendedor && row.precio !== null).map(row => ({
    vendedor: row.vendedor,
    carta: row.carta,
    precio: row.precio,
    cantidad: row.cantidad || 1,
    // Cardmarket calculates postage from destination/cart; no shipping cost is guessed.
    envio: null,
    sourceUrl: row.url,
  })) : [];
  if (!wants.length && !offers.length) {
    setStatus('Activa importar wants u ofertas. Para importar ofertas, la página debe mostrar el vendedor y el precio.', true);
    return;
  }
  $('import').disabled = true;
  setStatus('Enviando solo las filas previsualizadas al optimizador local…');
  try {
    const response = await fetch('http://127.0.0.1:8765/api/import', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ person, pageTitle: detected.listTitle, sourceUrl: detected.url, wants, offers }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'El optimizador local no aceptó la importación.');
    setStatus(`Importado localmente: ${wants.length} wants y ${offers.length} ofertas. Ya puedes cerrar esta ventana y revisar los datos en Cardpilot.`);
    window.setTimeout(() => api.tabs.create({ url: 'http://127.0.0.1:8765/' }), 500);
  } catch (error) {
    setStatus(`${error instanceof Error ? error.message : 'No se pudo conectar'}. Comprueba que Cardpilot está abierto en este ordenador.`, true);
  } finally {
    $('import').disabled = false;
  }
});

scanPage();
