'use strict';
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value == null ? '' : value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon = (name, extra) => '<svg class="icon ' + (extra || '') + '" aria-hidden="true"><use href="#i-' + name + '"/></svg>';
const number = (value) => new Intl.NumberFormat('ru-RU', {maximumFractionDigits:2}).format(Number(value) || 0);
const money = (value) => value === null || value === undefined || value === '' ? 'Цена не указана' : number(value) + ' ₽';
const date = (value, full) => { if (!value) return ''; const d = new Date(value); return isNaN(d) ? String(value) : new Intl.DateTimeFormat('ru-RU', {day:'2-digit',month:'2-digit',year:'numeric',...(full ? {hour:'2-digit',minute:'2-digit'} : {})}).format(d); };
const ui = {state:null, products:null, view:'grid', filters:{dimensions:'',width_min:'',width_max:'',height_min:'',height_max:'',depth_min:'',depth_max:'',dimension_order:'',dimension_unit:'',q:'',factory:'',collection:'',min:'',max:'',has_photo:'',sort:'name'}, page:1, request:0, product:null, currentPhoto:null, edit:false};
let imp = {step:1,files:[],inspect:null,preview:null,mode:'add',filter:'all'};
let restoreFile = null;
async function api(path, data, method) {
  let response;
  try { response = await fetch(path, {method:method || (data === undefined ? 'GET' : 'POST'), headers:data === undefined ? {} : {'Content-Type':'application/json'}, ...(data === undefined ? {} : {body:JSON.stringify(data)})}); }
  catch (e) { throw new Error('Не удалось связаться с каталогом. Убедитесь, что локальная программа запущена.'); }
  let result;
  try { result = await response.json(); } catch (e) { throw new Error('Программа вернула неожиданный ответ. Попробуйте ещё раз.'); }
  if (!response.ok) throw new Error(result.error || 'Не удалось выполнить операцию.');
  return result;
}
function showError(id, error) { const target=$(id); if(!target){if(error)toast(error.message || String(error),true);return;} target.textContent=error ? (error.message || String(error)) : ''; target.hidden=!error; }
function toast(message, error) {
  const node=document.createElement('div'); node.className='toast' + (error ? ' error' : '');
  node.innerHTML=icon(error?'close':'check') + '<span>' + esc(message) + '</span><button aria-label="Закрыть сообщение">' + icon('close') + '</button>';
  node.querySelector('button').onclick=()=>node.remove(); $('toastRegion').append(node); setTimeout(()=>node.remove(),6500);
}
function showDialog(id) { const dlg=$(id); if (!dlg.open) dlg.showModal(); document.body.style.overflow='hidden'; }
function closeDialog(dlg) { dlg.close(); if (!document.querySelector('dialog[open]')) document.body.style.overflow=''; }
function readFile(file) { return new Promise((resolve,reject)=>{const reader=new FileReader(); reader.onload=()=>resolve({name:file.name,content:String(reader.result).split(',')[1]}); reader.onerror=()=>reject(new Error('Не удалось прочитать файл «' + file.name + '».')); reader.readAsDataURL(file);}); }
function loading(button, yes, text) { if(yes){button.dataset.label=button.innerHTML;button.disabled=true;button.innerHTML='<span class="spinner"></span>' + (text || 'Подождите…');}else{button.disabled=false; if(button.dataset.label)button.innerHTML=button.dataset.label;delete button.dataset.label;} }
function furniture(name) {
  const n=String(name || '').toLowerCase(); let paths='';
  if (/диван|кресл|пуф|софа/.test(n)) paths='<path d="M35 60v-13a12 12 0 0 1 12-12h86a12 12 0 0 1 12 12v13" fill="#e1e6d8"/><path d="M26 59h14v32h100V59h14v38H26Z" fill="#d6ddca"/><path d="M40 63h100v20H40ZM84 64v18M35 98v9m110-9v9"/>';
  else if (/кроват|матрас|тахта/.test(n)) paths='<path d="M30 71V32h120v39" fill="#dde4d0"/><path d="M24 64h132v31H24Z" fill="#e6eadc"/><path d="M25 79h131M32 96v12m116-12v12M37 54h42v20H37ZM99 54h42v20H99Z"/>';
  else if (/стол|столешниц/.test(n)) paths='<path d="M20 44h140v12H20Z" fill="#dce3ce"/><path d="M34 56v53h9V56M137 56v53h9V56M45 67h90"/>';
  else if (/кухн|модул|фасад|мойк|корпус/.test(n)) paths='<path d="M20 46h140v56H20Z" fill="#dde4d1"/><path d="M15 43h150v7H15Z" fill="#d2dac4"/><path d="M64 51v50M116 51v50M30 60h24M77 61h26M77 77h26M127 61h22M75 54v40h30V54M27 103v6m126-6v6"/>';
  else if (/комод|тумб|пенал/.test(n)) paths='<path d="M40 26h100v76H40Z" fill="#dde4d1"/><path d="M44 48h92M44 73h92M76 38h28M76 60h28M76 85h28M49 103v7m82-7v7"/>';
  else if (/стул/.test(n)) paths='<path d="M61 17h58v45H61Z" fill="#dce3cf"/><path d="M56 62h68v11H56Z" fill="#dce3cf"/><path d="M64 74v35m52-35v35M64 91h52M65 24h50M65 42h50"/>';
  else paths='<path d="M48 13h84v91H48Z" fill="#dfe6d3"/><path d="M90 15v87M81 58v12m18-12v12M57 105v6m66-6v6"/>';
  return '<svg class="furniture-placeholder" viewBox="0 0 180 120" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 112h156" stroke="#d5ddca"/>' + paths + '</svg>';
}
function priceRule() {const s=ui.state?.settings || {markup_percent:20,fixed_add:1000};return 'Прайс + ' + number(s.markup_percent) + '% + ' + money(s.fixed_add);}
function renderState() {
  const s=ui.state, st=s.stats; $('statTotal').textContent=number(st.total);$('navTotal').textContent=number(st.total);$('statFactories').textContent=number(st.factories);
  $('statPhotos').innerHTML=number(st.with_photos) + '<small>из ' + number(st.total) + '</small>'; $('priceRule').textContent=priceRule();
  $('factoryCount').textContent=s.factories.length;
  $('factoryList').innerHTML=s.factories.length ? '<button class="factory-button ' + (!ui.filters.factory?'active':'') + '" data-factory="">Все фабрики<span>' + number(st.total) + '</span></button>' + s.factories.map(f=>'<button class="factory-button ' + (ui.filters.factory===f.name?'active':'') + '" data-factory="' + esc(f.name) + '"><b style="font-weight:inherit;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(f.name) + '</b><span>' + number(f.count) + '</span></button>').join('') : '<p class="sidebar-muted">Появятся после загрузки прайса</p>';
  const collections=s.collections.filter(c=>!ui.filters.factory || c.factory===ui.filters.factory);
  const names=[...new Set(collections.map(c=>c.name))];
  $('collectionOptions').innerHTML=names.map(c=>'<option value="' + esc(c) + '">').join('');if(document.activeElement!==$('collectionSelect'))$('collectionSelect').value=ui.filters.collection; $('mobileFactory').innerHTML='<option value="">Все фабрики</option>' + s.factories.map(f=>'<option value="' + esc(f.name) + '" ' + (ui.filters.factory===f.name?'selected':'') + '>' + esc(f.name) + ' · ' + number(f.count) + '</option>').join('');
  $('lastUpdated').textContent=st.last_import ? 'Последняя загрузка · ' + date(st.last_import) : 'Добавьте первый прайс';
  $('catalogTitle').textContent=ui.filters.factory || 'Все товары';$('folderSummary').textContent=s.folder?.path || 'Выберите папку для удобного обновления';
  updateFolderNotice();
}
async function refreshState() { ui.state=await api('/api/state');renderState();return ui.state; }
function filterActive() {return ['q','factory','collection','min','max','has_photo','dimensions','width_min','width_max','height_min','height_max','depth_min','depth_max'].some(k=>!!ui.filters[k]);}
async function loadProducts() {
  const request=++ui.request; $('catalogBody').setAttribute('aria-busy','true');showError('catalogError',null);
  const params=new URLSearchParams({...ui.filters,page:ui.page,page_size:36});
  try {const data=await api('/api/products?' + params);if(request!==ui.request)return;ui.products=data;renderProducts();}
  catch(e){if(request===ui.request){showError('catalogError',e);$('catalogBody').innerHTML='<div class="no-results">' + icon('folder') + '<h3>Каталог недоступен</h3><p>Проверьте, запущена ли программа, и попробуйте снова.</p><button class="button secondary" data-action="retry">Повторить</button></div>';}}
  finally {if(request===ui.request)$('catalogBody').setAttribute('aria-busy','false');}
}
function renderProducts() {
  const data=ui.products; renderDimensionHelp(data); const sizeFormatMissing=data.dimension_filter?.unrecognized > (data.dimension_filter?.empty || 0) && !data.dimension_filter.recognized && (!ui.filters.dimension_order || !ui.filters.dimension_unit); $('resultCount').textContent=number(data.total);$('resetFilters').hidden=!filterActive();
  const all=ui.state.stats.total;let html='';
  if(!all) html='<div class="empty-state"><div class="empty-art">' + furniture('шкаф') + '</div><div class="empty-copy"><p class="eyebrow">НАЧНЁМ С ПЕРВОГО ПРАЙСА</p><h3>Здесь будет ваша мебель</h3><p>Загрузите Excel-прайс. Мы соберём товары по фабрикам и коллекциям, а цены рассчитаем по вашему правилу.</p><button class="button primary" data-open="import">' + icon('upload') + 'Загрузить первый Excel</button><div class="empty-hint"><span>' + icon('check') + 'Дубли пропускаются</span><span>' + icon('camera') + 'Фото добавляются сюда</span></div></div></div>';
  else if(!data.items.length) html='<div class="no-results">' + icon('search') + '<h3>' + (sizeFormatMissing ? 'Выберите формат размеров' : 'Ничего не нашлось') + '</h3><p>' + (sizeFormatMissing ? 'Откройте «Размеры» и подтвердите порядок и единицы прайса. Значения «от / до» указаны в миллиметрах.' : 'Попробуйте другое название или расширьте фильтры.') + '</p><button class="button secondary" data-action="reset">Сбросить фильтры</button></div>';
  else if(ui.view==='grid') html='<div class="product-grid">' + data.items.map(productCard).join('') + '</div>';
  else html='<div class="table-container"><table class="product-table"><thead><tr><th>Фото</th><th>Товар</th><th>Фабрика</th><th>Размеры</th><th>Цвет</th><th>Цена продажи</th><th></th></tr></thead><tbody>' + data.items.map(p=>'<tr data-product="' + esc(p.id) + '" tabindex="0" role="button" aria-label="Открыть ' + esc(p.name) + '"><td>' + (p.photo_url?'<img class="table-photo" src="' + esc(p.photo_url) + '" alt="" loading="lazy">':'<div class="table-placeholder">' + furniture(p.name) + '</div>') + '</td><td class="table-name">' + esc(p.name) + '<small>' + esc([p.collection,p.sku].filter(Boolean).join(' · ')) + '</small></td><td>' + esc(p.factory) + '</td><td>' + esc(p.dimensions || '—') + '</td><td>' + esc(p.color || '—') + '</td><td><span class="sale-price">' + money(p.sale_price) + '</span><span class="base-price">Прайс: ' + money(p.base_price) + '</span></td><td>' + icon('chevron','small') + '</td></tr>').join('') + '</tbody></table></div>';
  $('catalogBody').innerHTML=html;
  const start=data.total ? ((data.page-1)*36+1):0;const end=Math.min(data.page*36,data.total);
  $('showingCount').textContent=data.total ? 'Показано ' + number(start) + '–' + number(end) + ' из ' + number(data.total) : '';
  renderPagination(data);
}
function productCard(p) {
  return '<button class="product-card" data-product="' + esc(p.id) + '" aria-label="Открыть ' + esc(p.name) + '"><div class="product-image"><span class="factory-badge">' + esc(p.factory) + '</span>' + (p.photo_url?'<img src="' + esc(p.photo_url) + '" alt="' + esc(p.name) + '" loading="lazy">':furniture(p.name) + '<span class="placeholder-caption">ФОТО ЕЩЁ НЕ ДОБАВЛЕНО</span>') + (p.photos?.length?'<span class="photo-count">' + icon('camera') + p.photos.length + '</span>':'') + '</div><div class="product-card-content"><span class="collection-label">' + esc(p.collection || 'Коллекция не указана') + '</span><h3 class="product-name">' + esc(p.name) + '</h3><p class="product-meta">' + esc([p.dimensions,p.color].filter(Boolean).join(' · ') || 'Размеры и цвет не указаны') + '</p><div class="product-price-block"><strong class="sale-price">' + money(p.sale_price) + '</strong><span class="base-price">Цена из прайса: ' + money(p.base_price) + '</span></div></div><span class="card-arrow">' + icon('chevron') + '</span></button>';
}
function renderPagination(data) {
  if(data.pages<2){$('pagination').innerHTML='';return;}
  let pages=[1,data.pages];for(let i=Math.max(1,data.page-1);i<=Math.min(data.pages,data.page+1);i++)pages.push(i);pages=[...new Set(pages)].sort((a,b)=>a-b);
  let html='<button class="page-button" data-page="' + (data.page-1) + '" ' + (data.page===1?'disabled':'') + ' aria-label="Предыдущая страница"><span style="transform:rotate(180deg)">' + icon('chevron') + '</span></button>';
  pages.forEach((p,i)=>{if(i && p-pages[i-1]>1)html+='<span>…</span>';html+='<button class="page-button ' + (p===data.page?'selected':'') + '" data-page="' + p + '" aria-label="Страница ' + p + '" ' + (p===data.page?'aria-current="page"':'') + '>' + p + '</button>';});
  $('pagination').innerHTML=html + '<button class="page-button" data-page="' + (data.page+1) + '" ' + (data.page===data.pages?'disabled':'') + ' aria-label="Следующая страница">' + icon('chevron') + '</button>';
}
function resetFilters() {$('dimensionPanel').reset();$('dimensionToggle').textContent='Размеры';ui.filters={dimensions:'',width_min:'',width_max:'',height_min:'',height_max:'',depth_min:'',depth_max:'',dimension_order:'',dimension_unit:'',q:'',factory:'',collection:'',min:'',max:'',has_photo:'',sort:'name'};ui.page=1;['searchInput','minPrice','maxPrice'].forEach(id=>$(id).value='');$('photoOnly').checked=false;$('sortSelect').value='name';renderState();loadProducts();}
function changeFilter(key,value) {ui.filters[key]=value;ui.page=1;if(key==='factory'){ui.filters.collection='';renderState();}loadProducts();}

async function openProduct(id) {
  const request=Date.now() + Math.random();ui.productRequest=request;ui.edit=false;ui.currentPhoto=null;ui.product=null;
  $('productContent').innerHTML='<div class="drawer-top"><span>Карточка товара</span><button class="icon-button" data-close aria-label="Закрыть">' + icon('close') + '</button></div><div class="drawer-loading"><span class="spinner"></span><p style="margin-top:14px">Открываем товар…</p></div>';
  showDialog('productDialog');
  try {const product=await api('/api/products/' + encodeURIComponent(id));if(ui.productRequest!==request)return;ui.product=product;renderProduct();}
  catch(e){if(ui.productRequest!==request)return;$('productContent').innerHTML='<div class="drawer-top"><span>Карточка товара</span><button class="icon-button" data-close aria-label="Закрыть">' + icon('close') + '</button></div><div class="drawer-main"><div class="notice error">' + esc(e.message) + '</div></div>';}
}
function renderProduct() {
  const p=ui.product; if(!p)return; const photos=p.photos || [];
  const photo=photos.find(x=>x.id===ui.currentPhoto) || photos[0];ui.currentPhoto=photo?.id || null;
  const fact=(title,value)=>'<div class="drawer-fact"><span>' + title + '</span><strong>' + esc(value || 'Не указано') + '</strong></div>';
  $('productContent').innerHTML='<div class="drawer-top"><span>Карточка товара · ' + esc(p.factory) + '</span><button class="icon-button" data-close aria-label="Закрыть">' + icon('close') + '</button></div><div class="drawer-main"><div class="drawer-gallery" id="drawerGallery">' + (photo?'<img src="' + esc(photo.url) + '" alt="' + esc(p.name) + '">':furniture(p.name) + '<span class="placeholder-caption">Добавьте фотографию этого товара</span>') + '</div>' +
    '<div class="gallery-thumbs">' + photos.map(x=>'<button class="gallery-thumb ' + (x.id===ui.currentPhoto?'active':'') + '" data-photo-select="' + esc(x.id) + '" aria-label="Посмотреть фото ' + esc(x.original_name) + '"><img src="' + esc(x.url) + '" alt=""></button>').join('') + '<button class="gallery-thumb gallery-add" data-action="add-photo" aria-label="Добавить фотографию">' + icon('plus') + '</button></div><p class="drawer-eyebrow">' + esc(p.collection || p.factory) + '</p><h2 id="productTitle">' + esc(p.name) + '</h2><div class="drawer-price"><div><strong class="sale-price">' + money(p.sale_price) + '</strong><span class="base-price">Цена из прайса: ' + money(p.base_price) + '</span></div><span class="price-rule-tag">+' + number(ui.state.settings.markup_percent) + '% · +' + money(ui.state.settings.fixed_add) + '</span></div><div class="drawer-facts">' + fact('Фабрика',p.factory) + fact('Коллекция',p.collection) + fact('Размеры',p.dimensions) + fact('Цвет',p.color) + fact('Артикул',p.sku) + (p.variant?fact('Комплектация',p.variant):'') + '</div>' +
    '<section class="drawer-section"><div class="drawer-section-title"><h3>Информация о товаре</h3><button class="text-button" data-action="edit-product">' + icon('edit','small') + (ui.edit?'Свернуть':'Редактировать') + '</button></div><p class="photo-help" style="margin:0">Ваши изменения сохраняются при следующей загрузке прайса.</p>' + (ui.edit?editProductForm(p):'') + '</section>' +
    '<section class="drawer-section"><div class="drawer-section-title"><h3>Фотографии</h3><span class="muted">' + photos.length + '</span></div><label class="photo-upload" id="photoDropzone" tabindex="0" role="button" aria-label="Добавить фотографии">' + icon('camera') + '<span><strong>Добавить фотографии</strong><small>Выберите или перетащите · JPG, PNG, WEBP</small></span><input id="photoInput" type="file" accept="image/jpeg,image/png,image/webp" multiple hidden></label><div id="photoError" class="notice error" hidden role="alert"></div>' +
    (photos.length?'<div class="photo-manager">' + photos.map(x=>'<div class="photo-manager-card"><img src="' + esc(x.url) + '" alt="' + esc(x.original_name) + '"><div class="photo-manager-controls"><button class="primary-photo-button ' + (x.is_primary?'active':'') + '" data-photo-primary="' + esc(x.id) + '" ' + (x.is_primary?'disabled':'') + '>' + (x.is_primary?icon('check') + 'Основное':'Сделать основным') + '</button><button class="icon-button" data-photo-delete="' + esc(x.id) + '" aria-label="Удалить фотографию ' + esc(x.original_name) + '">' + icon('trash') + '</button></div></div>').join('') + '</div>':'') + '<p class="photo-help">Фото хранятся в вашей базе и остаются на месте после обновления Excel. Встроенные фотографии из прайса добавляются автоматически; свои фотографии можно загрузить здесь.</p></section>' +
    '<details class="source-details"><summary>' + icon('file') + 'Источник данных ' + icon('down') + '</summary><dl><dt>Файл</dt><dd>' + esc(p.source_file) + '</dd><dt>Лист</dt><dd>' + esc(p.source_sheet) + '</dd><dt>Строка</dt><dd>' + esc(p.source_row) + '</dd><dt>Загружено</dt><dd>' + esc(date(p.imported_at,true)) + '</dd></dl></details></div>';
  $('photoInput').addEventListener('change',e=>uploadPhotos([...e.target.files]));
  const drop=$('photoDropzone');drop.addEventListener('dragover',e=>{e.preventDefault();drop.style.background='#e8f0df';});drop.addEventListener('dragleave',()=>drop.style.background='');drop.addEventListener('drop',e=>{e.preventDefault();drop.style.background='';uploadPhotos([...e.dataTransfer.files]);});
  if(ui.edit)$('productEditForm').addEventListener('submit',saveProduct);
}
function editProductForm(p) {
  const input=(key,label,full)=>'<label class="field ' + (full?'full':'') + '">' + label + '<input name="' + key + '" value="' + esc(p[key] || '') + '" ' + (key==='name' || key==='factory'?'required':'') + '></label>';
  return '<form id="productEditForm" class="edit-form"><div class="form-grid">' + input('name','Наименование',true) + input('factory','Фабрика') + input('collection','Коллекция') + input('sku','Артикул') + input('color','Цвет') + input('dimensions','Размеры',true) + input('variant','Комплектация',true) + '</div><div id="productEditError" class="notice error" hidden role="alert"></div><div class="edit-actions"><button class="button small secondary" type="button" data-action="cancel-edit">Отмена</button><button class="button small primary" type="submit">Сохранить</button></div></form>';
}
async function saveProduct(e) {
  e.preventDefault();const button=e.target.querySelector('[type=submit]');loading(button,true,'Сохраняем…');showError('productEditError',null);
  try {
    const product=ui.product, changes={};
    for(const [key,value] of new FormData(e.target)) {
      const next=String(value).trim();
      if(next!==String(product[key] == null ? '' : product[key]).trim())changes[key]=next;
    }
    if(!Object.keys(changes).length){ui.edit=false;renderProduct();toast('Изменений нет');return;}
    const updated=await api('/api/products/' + product.id,changes,'PATCH');
    if(ui.product?.id===product.id){ui.product=updated;ui.edit=false;renderProduct();}
    await refreshState();await loadProducts();toast('Изменения сохранены');
  }
  catch(error){showError('productEditError',error);}finally{loading(button,false);}
}
async function uploadPhotos(files) {
  if(!files.length)return;showError('photoError',null);
  if(files.some(f=>!['image/jpeg','image/png','image/webp'].includes(f.type))){showError('photoError','Поддерживаются фотографии JPG, PNG и WEBP.');return;}
  if(files.some(f=>f.size>15*1024*1024)){showError('photoError','Одна фотография должна быть меньше 15 МБ.');return;}
  const drop=$('photoDropzone');drop.style.pointerEvents='none';drop.querySelector('strong').textContent='Загружаем фотографии…';
  try {const id=ui.product.id;const encoded=[];for(const file of files)encoded.push(await readFile(file));const product=await api('/api/products/' + id + '/photos',{files:encoded});if(ui.product?.id===id){ui.product=product;renderProduct();}await refreshState();await loadProducts();toast(files.length===1?'Фотография добавлена':'Фотографии добавлены');}
  catch(e){showError('photoError',e);drop.style.pointerEvents='';drop.querySelector('strong').textContent='Добавить фотографии';}
}
function confirmAction(title, description, label) {
  return new Promise(resolve=>{
    let dlg=$('confirmDialog');if(!dlg){dlg=document.createElement('dialog');dlg.id='confirmDialog';dlg.className='modal compact-modal';document.body.append(dlg);}
    dlg.innerHTML='<div class="modal-header"><h2>' + esc(title) + '</h2><button class="icon-button" data-cancel aria-label="Закрыть">' + icon('close') + '</button></div><div class="modal-body"><p class="body-description" style="margin:0">' + esc(description) + '</p></div><div class="modal-actions"><button class="button secondary" data-cancel>Отмена</button><button class="button primary" data-confirm>' + esc(label) + '</button></div>';
    const done=(value)=>{closeDialog(dlg);resolve(value);};dlg.querySelectorAll('[data-cancel]').forEach(b=>b.onclick=()=>done(false));dlg.querySelector('[data-confirm]').onclick=()=>done(true);dlg.oncancel=e=>{e.preventDefault();done(false);};showDialog('confirmDialog');
  });
}
async function deletePhoto(id) {
  if(!await confirmAction('Удалить фотографию?','Фотография будет удалена из карточки товара. Вы сможете загрузить её снова.','Удалить фото'))return;
  try {await api('/api/photos/' + id,undefined,'DELETE');ui.product=await api('/api/products/' + ui.product.id);renderProduct();await refreshState();await loadProducts();toast('Фотография удалена');}
  catch(e){showError('photoError',e);}
}
async function primaryPhoto(id) {
  try{await api('/api/photos/' + id,{primary:true},'PATCH');ui.product=await api('/api/products/' + ui.product.id);ui.currentPhoto=id;renderProduct();await loadProducts();toast('Основное фото обновлено');}
  catch(e){showError('photoError',e);}
}
function openImport(result, mode) {imp={step:result?2:1,files:[],inspect:result || null,preview:null,mode:mode || 'add',filter:'all',folderPath:''};showError('importError',null);renderImport();showDialog('importDialog');}
function renderImport() {
  [1,2,3].forEach(i=>{$('step'+i).className='wizard-step ' + (i===imp.step?'current':i<imp.step?'complete':'');});
  $('importBack').hidden=imp.step===1;$('importNext').disabled=false;
  $('importActionHint').textContent=imp.step===1?'Excel-файлы остаются без изменений':imp.step===2?'Профиль запомнится для следующих загрузок':imp.preview?.counts.issues?'Позиции с замечаниями будут пропущены':'Фотографии существующих товаров сохранятся';
  $('importNext').innerHTML=(imp.step===1?'Проверить файлы':imp.step===2?'Предпросмотр':(imp.preview?.counts.new || imp.preview?.counts.updated)?'Сохранить в каталог':'Завершить проверку') + icon(imp.step===3?'check':'arrow');
  if(imp.step===1)renderImportFiles();else if(imp.step===2)renderImportMapping();else renderImportPreview();
  $('importDialog').scrollTop=0;
}
function renderImportFiles() {
  $('importContent').innerHTML='<label class="dropzone" id="excelDropzone" tabindex="0">' + icon('upload') + '<h3>Перетащите сюда Excel-прайсы</h3><p>Один или несколько файлов · .xls и .xlsx</p><span class="button secondary">' + icon('folder') + 'Выбрать файлы</span><input id="excelInput" type="file" accept=".xls,.xlsx" multiple hidden></label><div id="selectedFiles" class="selected-file-list"></div><div class="import-mode"><h3 class="form-caption">Что сделать с каталогом?</h3><label class="mode-card"><input type="radio" name="importMode" value="add" ' + (imp.mode==='add'?'checked':'') + '><span><strong>Добавить только новые</strong><small>Новые коллекции и товары добавятся. Дубли будут пропущены, цены существующих товаров сохранятся.</small></span></label><label class="mode-card"><input type="radio" name="importMode" value="update" ' + (imp.mode==='update'?'checked':'') + '><span><strong>Обновить цены и добавить новые</strong><small>Новые цены заменят исходные цены из прайса. Фото и ваши правки останутся.</small></span></label></div><details class="import-path"><summary>' + icon('folder') + 'Загрузить из папки на компьютере ' + icon('down') + '</summary><label class="field">Полный путь к папке<input id="importFolderPath" value="' + esc(imp.folderPath) + '" placeholder="C:\\Users\\…\\Прайсы"></label><p class="muted small-text">Будут прочитаны Excel-файлы непосредственно из этой папки.</p></details>';
  renderSelectedFiles();$('excelInput').onchange=e=>addExcelFiles([...e.target.files]);
  const drop=$('excelDropzone');drop.onkeydown=e=>{if(e.key==='Enter' || e.key===' '){e.preventDefault();$('excelInput').click();}};
  drop.ondragover=e=>{e.preventDefault();drop.classList.add('dragover');};drop.ondragleave=()=>drop.classList.remove('dragover');drop.ondrop=e=>{e.preventDefault();drop.classList.remove('dragover');addExcelFiles([...e.dataTransfer.files]);};
  document.querySelectorAll('[name=importMode]').forEach(r=>r.onchange=()=>imp.mode=r.value);
  $('importFolderPath').oninput=e=>imp.folderPath=e.target.value;
}
function addExcelFiles(files) {
  showError('importError',null);const valid=files.filter(f=>/\.(xls|xlsx)$/i.test(f.name));
  if(valid.length!==files.length)showError('importError','Поддерживаются только Excel-файлы .xls и .xlsx.');
  if(valid.length)imp.inspect=null;valid.forEach(f=>{const index=imp.files.findIndex(x=>x.name===f.name);if(index>=0)imp.files[index]=f;else imp.files.push(f);});renderSelectedFiles();
}
function renderSelectedFiles() {
  $('selectedFiles').innerHTML=imp.files.map((f,i)=>'<div class="selected-file">' + icon('file') + '<span title="' + esc(f.name) + '">' + esc(f.name) + '</span><small>' + number(f.size/1024/1024) + ' МБ</small><button class="icon-button" data-file-remove="' + i + '" aria-label="Убрать ' + esc(f.name) + '">' + icon('close') + '</button></div>').join('');
}

function warningsHtml(warnings) {return warnings?.length?'<div class="notice">' + warnings.map(w=>'<div>' + esc(w.message || w) + '</div>').join('') + '</div>':'';}
function columnLabel(header) {
  const column=String(header.column || '').trim();
  let label=String(header.label || '').replace(/\s+/g,' ').trim();
  if(!label || label.toLocaleUpperCase('ru-RU')===column.toLocaleUpperCase('ru-RU'))return 'Столбец ' + column;
  label=label.replace(/^наимено[-\s]*вание$/i,'Наименование');
  if(label===label.toLocaleUpperCase('ru-RU')) {
    const abbreviations=new Set(['РРЦ','ОПТ','МДФ','ЛДСП','ДСП','SV','NN']);
    label=label.split(' ').map(word=>abbreviations.has(word)?word:word.toLocaleLowerCase('ru-RU')).join(' ');
    label=label.charAt(0).toLocaleUpperCase('ru-RU') + label.slice(1);
  }
  return label + ' (' + column + ')';
}
function mappingOptions(sheet, key) {
  const current=sheet.mapping?.[key] || '';
  let html='<option value="">Не использовать</option>';
  const heads=sheet.headers || [];
  if(current && !heads.some(h=>h.column===current))html+='<option value="' + esc(current) + '" selected>' + 'Составное поле (' + esc(current) + ')</option>';
  return html + heads.map(h=>'<option value="' + esc(h.column) + '" ' + (current===h.column?'selected':'') + '>' + esc(columnLabel(h)) + '</option>').join('');
}
function renderImportMapping() {
  const labels={name:'Наименование *',price:'Исходная цена *',sku:'Артикул',color:'Цвет',dimensions:'Размеры целиком',collection:'Коллекция',width:'Ширина',height:'Высота',depth:'Глубина'};
  let html='<p class="body-description">Проверьте фабрику и отметьте листы с товарами.</p><div class="notice" style="margin-top:-8px">Выберите столбцы с наименованием и исходной ценой. Буква в скобках соответствует столбцу Excel. Остальные поля можно не использовать.<br>Встроенные фотографии добавятся автоматически; если их нет, загрузите через карточку товара.</div><datalist id="factorySuggestions">' + [...new Set([...(ui.state?.factories || []).map(f=>f.name),...imp.inspect.files.map(f=>f.factory)].filter(Boolean))].map(f=>'<option value="' + esc(f) + '">').join('') + '</datalist>';
  imp.inspect.files.forEach((file,fi)=>{
    html+='<section class="file-profile"><div class="file-profile-heading">' + icon('file') + '<span>' + esc(file.name) + '</span>' + (file.profile_used?'<span class="status-tag">Профиль найден</span>':'') + '</div><label class="field file-factory">Фабрика для файла<input data-file-factory="' + fi + '" value="' + esc(file.factory) + '" list="factorySuggestions" placeholder="Укажите название фабрики" required></label>' + warningsHtml(file.warnings);
    file.sheets.forEach((sheet,si)=>{
      const headers=sheet.headers || [];
      html+='<details class="sheet-card" ' + (sheet.selected && si===file.sheets.findIndex(x=>x.selected)?'open':'') + '><summary><label><input type="checkbox" data-sheet-selected data-fi="' + fi + '" data-si="' + si + '" ' + (sheet.selected?'checked':'') + '>' + esc(sheet.name) + '</label><small>' + number(sheet.count) + ' позиций</small>' + icon('chevron') + '</summary><div class="sheet-body">' +
        (sheet.factory?'<label class="field file-factory">Фабрика этого листа<input data-sheet-factory data-fi="' + fi + '" data-si="' + si + '" value="' + esc(sheet.factory) + '" list="factorySuggestions"></label>':'') +
        '<div class="mapping-grid">' + Object.entries(labels).map(([key,label])=>'<label class="field">' + label + '<select data-mapping="' + key + '" data-fi="' + fi + '" data-si="' + si + '">' + mappingOptions(sheet,key) + '</select></label>').join('') + '</div>';
      if(sheet.sample_rows?.length)html+='<p class="sample-title">Первые строки листа</p><div class="sample-table-container"><table class="sample-table"><thead><tr>' + headers.map(h=>'<th title="' + esc(columnLabel(h)) + '">' + esc(columnLabel(h)) + '</th>').join('') + '</tr></thead><tbody>' + sheet.sample_rows.slice(0,5).map(row=>'<tr>' + headers.map(h=>'<td title="' + esc(row[h.column] || '') + '">' + esc(row[h.column] || '') + '</td>').join('') + '</tr>').join('') + '</tbody></table></div>';
      html+='</div></details>';
    });html+='</section>';
  });$('importContent').innerHTML=html;
}
function collectMappings() {
  const files=JSON.parse(JSON.stringify(imp.inspect.files));
  document.querySelectorAll('[data-file-factory]').forEach(el=>files[Number(el.dataset.fileFactory)].factory=el.value.trim());
  document.querySelectorAll('[data-sheet-selected]').forEach(el=>files[Number(el.dataset.fi)].sheets[Number(el.dataset.si)].selected=el.checked);
  document.querySelectorAll('[data-sheet-factory]').forEach(el=>files[Number(el.dataset.fi)].sheets[Number(el.dataset.si)].factory=el.value.trim());
  document.querySelectorAll('[data-mapping]').forEach(el=>files[Number(el.dataset.fi)].sheets[Number(el.dataset.si)].mapping[el.dataset.mapping]=el.value || null);
  return files;
}
function calcPrice(base,settings) {
  if(base===null || base===undefined || base==='')return null;
  let value=Number(base)*(1+Number(settings.markup_percent)/100)+Number(settings.fixed_add);
  const unit={none:.01,ruble:1,ten:10,hundred:100}[settings.rounding] || 1;
  return Math.round((value+Number.EPSILON)/unit)*unit;
}
function renderImportPreview() {
  const p=imp.preview, c=p.counts;
  const statuses={all:'Все позиции',new:'Новые',updated:'Обновления',duplicate:'Дубли',issue:'Замечания'};
  let html='<div class="preview-counts">' + [['new','Новых товаров'],['updated','Обновлений'],['duplicates','Дублей пропущено'],['issues','На проверку']].map(([key,title])=>'<div class="preview-count ' + (key==='issues'?'issue':'') + '"><strong>' + number(c[key]) + '</strong><span>' + title + '</span></div>').join('') + '</div>';
  if(p.photos?.new>0)html+='<div class="notice photo-import-notice">' + icon('camera') + '<span>Будет добавлено ' + number(p.photos.new) + ' фото к ' + number(p.photos.products) + ' позициям</span></div>';
  html+=warningsHtml(p.warnings);
  if(c.issues)html+='<div class="notice">Позиции с замечаниями не будут загружены. Проверьте исходные строки и выбор столбцов; остальные товары можно сохранить.<br><a class="text-button" href="/api/import/report/' + esc(p.session_id) + '" download>' + icon('file','small') + 'Скачать список проблем (CSV)</a></div>';
  if(!c.new && !c.updated && !c.duplicates && !c.issues)html+='<div class="notice">Товары не найдены. Вернитесь к выбору столбцов и проверьте наименование, цену и выбранные листы.</div>';
  html+='<div class="preview-filters">' + Object.entries(statuses).map(([key,title])=>'<button class="preview-filter ' + (imp.filter===key?'active':'') + '" data-preview-filter="' + key + '">' + title + '</button>').join('') + '</div>';
  const items=p.items.filter(x=>imp.filter==='all' || x.status===imp.filter);
  html+='<div class="sample-table-container" style="max-height:330px"><table class="preview-table"><thead><tr><th>Товар</th><th>Прайс</th><th>Цена продажи</th><th>Результат</th></tr></thead><tbody>' + items.map(x=>'<tr><td class="preview-name"><div class="preview-product">' + (x.excel_photo_url?'<figure class="preview-excel-photo"><img src="' + esc(x.excel_photo_url) + '" alt="Фото из Excel" loading="lazy"><figcaption>Фото из Excel' + (x.excel_photos_count>1?' · ' + number(x.excel_photos_count):'') + '</figcaption></figure>':'') + '<div class="preview-product-info">' + esc(x.name || 'Строка без названия') + '<small>' + esc([x.factory,x.collection,x.dimensions,x.color].filter(Boolean).join(' · ')) + '</small>' + (x.status==='issue'?'<small style="color:#a38a52">' + esc(x.message) + '</small>':'') + '<small>' + esc([x.source_file, x.source_sheet ? 'Лист: ' + x.source_sheet : '', x.source_row ? 'Строка: ' + x.source_row : ''].filter(Boolean).join(' · ')) + '</small></div></div></td><td style="white-space:nowrap">' + money(x.base_price) + '</td><td style="white-space:nowrap">' + (x.status==='duplicate'?'Без изменений':x.status==='issue'?'На проверку':money(x.sale_price === undefined?calcPrice(x.base_price,ui.state.settings):x.sale_price)) + '</td><td><span class="status-tag ' + esc(x.status) + '" title="' + esc(x.message) + '">' + (statuses[x.status] || esc(x.status)) + '</span></td></tr>').join('') + (!items.length?'<tr><td colspan="4" style="text-align:center;padding:30px">Таких позиций в предпросмотре нет</td></tr>':'') + '</tbody></table></div>';
  if(p.total_rows)html+='<p class="preview-limit">Показаны примеры: ' + number(p.items.length) + ' из ' + number(p.total_rows) + ' строк. Итоги выше учитывают все строки.</p>';
  $('importContent').innerHTML=html;
}
async function nextImport() {
  const context=imp;const button=$('importNext');showError('importError',null);loading(button,true,imp.step===3?'Сохраняем…':'Читаем прайс…');
  try {
    if(imp.step===1){
      if(!imp.files.length && !imp.folderPath?.trim())throw new Error('Выберите Excel-файлы или укажите папку с прайсами.');
      let payload;
      if(imp.files.length){const files=[];for(const file of imp.files)files.push(await readFile(file));payload={files};}else payload={folder:imp.folderPath.trim()};
      const inspection=await api('/api/import/inspect',payload);if(imp!==context)return;imp.inspect=inspection;imp.step=2;
    }else if(imp.step===2){
      const files=collectMappings();
      let selected=0;
      files.forEach(file=>{if(!file.factory.trim())throw new Error('Укажите фабрику для файла «' + file.name + '».');file.sheets.filter(s=>s.selected).forEach(sheet=>{selected++;if(!sheet.mapping.name || !sheet.mapping.price)throw new Error('На листе «' + sheet.name + '» выберите столбцы наименования и исходной цены.');});});
      if(!selected)throw new Error('Выберите хотя бы один лист с товарами.');
      imp.inspect.files=files;
      const preview=await api('/api/import/preview',{session_id:imp.inspect.session_id,mode:imp.mode,files});if(imp!==context)return;imp.preview=preview;imp.step=3;imp.filter='all';
    }else{
      const result=await api('/api/import/commit',{session_id:imp.inspect.session_id});if(imp===context)closeDialog($('importDialog'));await refreshState();ui.page=1;await loadProducts();toast('Готово: добавлено ' + number(result.added) + ', обновлено ' + number(result.updated) + ', дублей пропущено ' + number(result.duplicates) + (result.photos_added>0?', фото добавлено ' + number(result.photos_added):''));return;
    }
    loading(button,false);renderImport();
  }catch(e){if(imp===context)showError('importError',e);}finally{if(imp===context)loading(button,false);}
}
function importBack() {
  if(imp.step===3)imp.step=2;
  else {imp.inspect.files=collectMappings();imp.step=1;}
  showError('importError',null);renderImport();
}
function openSettings() {
  const settings=ui.state.settings,form=$('settingsForm');
  Object.keys(settings).forEach(key=>{if(form.elements[key])form.elements[key].value=settings[key];});showError('settingsError',null);settingsExample();showDialog('settingsDialog');
}
function settingsExample() {const f=$('settingsForm');$('settingsExample').textContent=money(calcPrice(10000,{markup_percent:f.elements.markup_percent.value,fixed_add:f.elements.fixed_add.value,rounding:f.elements.rounding.value}));}
async function saveSettings(e) {
  e.preventDefault();const button=e.target.querySelector('[type=submit]');loading(button,true,'Сохраняем…');showError('settingsError',null);
  try{const values=Object.fromEntries(new FormData(e.target));values.markup_percent=Number(values.markup_percent);values.fixed_add=Number(values.fixed_add);await api('/api/settings',values);await refreshState();await loadProducts();if(ui.product && $('productDialog').open){ui.product=await api('/api/products/' + ui.product.id);renderProduct();}closeDialog($('settingsDialog'));toast('Правило цены сохранено. Все цены пересчитаны.');}
  catch(error){showError('settingsError',error);}finally{loading(button,false);}
}
function openHistory() {
  const imports=ui.state.imports || [];
  $('historyContent').innerHTML=imports.length?imports.map(entry=>'<article class="history-item">' + icon('file') + '<div class="history-body"><h3>' + esc((entry.files || []).map(f=>typeof f==='string'?f:f.name).join(', ') || 'Импорт прайса') + '</h3><p class="history-date">' + date(entry.created_at,true) + ' · ' + (entry.mode==='update'?'Обновление цен и новые товары':'Только новые товары') + '</p><div class="history-stats"><span><b>' + number(entry.added) + '</b> добавлено</span><span><b>' + number(entry.updated) + '</b> обновлено</span><span><b>' + number(entry.duplicates) + '</b> дублей</span><span><b>' + number(entry.issues) + '</b> замечаний</span></div>' + (entry.issues?'<a class="text-button" style="font-size:10px;margin-top:8px" href="/api/imports/' + esc(entry.id) + '/report" download>' + icon('file','small') + 'Скачать список проблем (CSV)</a><details style="margin-top:10px"><summary class="muted" style="cursor:pointer;font-size:10px">Посмотреть замечания</summary>' + (entry.details || []).filter(d=>d.status==='issue').slice(0,25).map(d=>'<p class="muted" style="margin-top:7px;font-size:10px">' + esc(d.name || d.source_file || 'Строка') + ': ' + esc(d.message || 'Требуется проверка') + '<br><span style="font-size:9px">' + esc([d.source_file,d.source_sheet?'Лист: ' + d.source_sheet:'',d.source_row?'Строка: ' + d.source_row:''].filter(Boolean).join(' · ')) + '</span></p>').join('') + '</details>':'') + '</div></article>').join(''):'<div class="history-empty">' + icon('history') + '<h3>Загрузок ещё не было</h3><p class="muted">После первого импорта здесь появятся его результаты.</p></div>';
  showDialog('historyDialog');
}
function openFolder() {
  const f=ui.state.folder || {},form=$('folderForm');form.elements.path.value=f.path || '';form.elements.mode.value=f.mode || 'add';form.elements.watch.checked=!!f.watch;showError('folderError',null);updateFolderNotice();showDialog('folderDialog');
}
function updateFolderNotice() {
  const f=ui.state?.folder || {},target=$('folderStatus');
  const statuses={idle:'Папка подключена. Автозагрузка выключена.',watching:'Автозагрузка включена. Папка проверяется, пока программа запущена.',review:'Есть новые файлы, которые нужно проверить.',scanning:'Проверяем папку…'};
  target.hidden=!f.path;target.innerHTML=esc(f.error || statuses[f.status] || 'Папка подключена.') + (f.pending_session?'<br><button class="text-button" data-action="review-folder">Проверить прайс ' + icon('arrow','small') + '</button>':'') + (f.last_scan?'<p class="small-text">Последняя проверка: ' + esc(date(f.last_scan,true)) + '</p>':'');
  const last=$('lastUpdated');if(f.pending_session){last.innerHTML='<button class="text-button" data-action="review-folder" style="font-size:10px">Новый прайс ждёт проверки ' + icon('arrow','small') + '</button>';}
}
async function saveFolder(e) {
  e.preventDefault();const button=e.target.querySelector('[type=submit]');loading(button,true,'Сохраняем…');showError('folderError',null);
  try{const f=e.target;await api('/api/folder',{path:f.elements.path.value.trim(),mode:f.elements.mode.value,watch:f.elements.watch.checked});await refreshState();toast('Папка с прайсами сохранена');}
  catch(error){showError('folderError',error);}finally{loading(button,false);}
}
async function scanFolder() {
  const button=$('folderScan'),form=$('folderForm');loading(button,true,'Читаем…');showError('folderError',null);
  try {await api('/api/folder',{path:form.elements.path.value.trim(),mode:form.elements.mode.value,watch:form.elements.watch.checked});const result=await api('/api/folder/scan',{});await refreshState();closeDialog($('folderDialog'));if($('settingsDialog').open)closeDialog($('settingsDialog'));openImport(result,form.elements.mode.value);}
  catch(e){showError('folderError',e);}finally{loading(button,false);}
}
async function reviewFolder() {
  try {const id=ui.state.folder?.pending_session;if(!id)return;const result=await api('/api/import/session/' + id);if($('folderDialog').open)closeDialog($('folderDialog'));if($('settingsDialog').open)closeDialog($('settingsDialog'));openImport(result,ui.state.folder.mode);}
  catch(e){toast(e.message,true);}
}
function openBackup() {restoreFile=null;$('restoreInput').value='';$('restoreDetails').hidden=true;$('restoreConfirm').checked=false;$('restoreButton').disabled=true;showError('backupError',null);showDialog('backupDialog');}
async function restoreBackup() {
  if(!restoreFile || !$('restoreConfirm').checked)return;
  const button=$('restoreButton');loading(button,true,'Восстанавливаем…');showError('backupError',null);
  try {const payload=await readFile(restoreFile);await api('/api/restore',payload);ui.product=null;ui.currentPhoto=null;ui.edit=false;closeDialog($('backupDialog'));await refreshState();resetFilters();toast('Каталог восстановлен. Предыдущая база сохранена в отдельной копии.');}
  catch(e){showError('backupError',e);}finally{loading(button,false);}
}

function openNamed(name) {
  if(name==='import'){openImport();return;}
  if(!ui.state){toast('Каталог ещё не загрузился. Попробуйте снова через несколько секунд.',true);return;}
  if(name==='settings')openSettings();else if(name==='backup')openBackup();else if(name==='history')openHistory();else if(name==='folder')openFolder();else if(name==='update')openUpdate();
}
document.addEventListener('click',event=>{
  const target=event.target instanceof Element ? event.target : null;if(!target)return;
  const close=target.closest('[data-close]');if(close){closeDialog(close.closest('dialog'));return;}
  const opener=target.closest('[data-open]');if(opener){openNamed(opener.dataset.open);return;}
  const product=target.closest('[data-product]');if(product){openProduct(product.dataset.product);return;}
  const factory=target.closest('[data-factory]');if(factory){changeFilter('factory',factory.dataset.factory);return;}
  const page=target.closest('[data-page]');if(page && !page.disabled){ui.page=Number(page.dataset.page);loadProducts();$('catalogTitle').scrollIntoView({block:'start',behavior:'smooth'});return;}
  const remove=target.closest('[data-file-remove]');if(remove){imp.files.splice(Number(remove.dataset.fileRemove),1);imp.inspect=null;renderSelectedFiles();return;}
  const preview=target.closest('[data-preview-filter]');if(preview){imp.filter=preview.dataset.previewFilter;renderImportPreview();return;}
  const photo=target.closest('[data-photo-select]');if(photo){ui.currentPhoto=photo.dataset.photoSelect;const selected=ui.product.photos.find(p=>p.id===ui.currentPhoto);if(selected){$('drawerGallery').innerHTML='<img src="' + esc(selected.url) + '" alt="' + esc(ui.product.name) + '">';document.querySelectorAll('[data-photo-select]').forEach(el=>el.classList.toggle('active',el.dataset.photoSelect===ui.currentPhoto));}return;}
  const primary=target.closest('[data-photo-primary]');if(primary && !primary.disabled){primaryPhoto(primary.dataset.photoPrimary);return;}
  const del=target.closest('[data-photo-delete]');if(del){deletePhoto(del.dataset.photoDelete);return;}
  const action=target.closest('[data-action]')?.dataset.action;
  if(action==='reset')resetFilters();else if(action==='retry')bootstrap();else if(action==='add-photo')$('photoInput')?.click();
  else if(action==='edit-product'){ui.edit=!ui.edit;renderProduct();if(ui.edit)$('productEditForm').scrollIntoView({block:'nearest',behavior:'smooth'});}
  else if(action==='cancel-edit'){ui.edit=false;renderProduct();}
  else if(action==='review-folder')reviewFolder();
});
document.addEventListener('keydown',event=>{
  if((event.key==='Enter' || event.key===' ') && event.target.matches('label.file-button, label.photo-upload')){event.preventDefault();event.target.querySelector('input[type=file]')?.click();}
  if(event.key==='/' && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) && !document.querySelector('dialog[open]')){event.preventDefault();$('searchInput').focus();}
  if((event.key==='Enter' || event.key===' ') && event.target.matches('tr[data-product]')){event.preventDefault();openProduct(event.target.dataset.product);}
});
document.querySelectorAll('dialog').forEach(dlg=>{
  dlg.addEventListener('close',()=>{if(!document.querySelector('dialog[open]'))document.body.style.overflow='';});
  dlg.addEventListener('click',e=>{if(e.target!==dlg)return;const r=dlg.getBoundingClientRect();if(e.clientX<r.left || e.clientX>r.right || e.clientY<r.top || e.clientY>r.bottom)closeDialog(dlg);});
});
let searchTimer=null,priceTimer=null;
$('searchInput').addEventListener('input',e=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>changeFilter('q',e.target.value.trim()),220);});
['minPrice','maxPrice'].forEach((id,i)=>$(id).addEventListener('input',e=>{clearTimeout(priceTimer);priceTimer=setTimeout(()=>{ui.filters.min=$('minPrice').value;ui.filters.max=$('maxPrice').value;ui.page=1;loadProducts();},300);}));
$('sortSelect').onchange=e=>changeFilter('sort',e.target.value);
$('collectionSelect').onchange=e=>changeFilter('collection',e.target.value);
$('photoOnly').onchange=e=>changeFilter('has_photo',e.target.checked?'true':'');
$('resetFilters').onclick=resetFilters;$('navCatalog').onclick=resetFilters;
function setView(view){ui.view=view;try{localStorage.setItem('catalogView',view);}catch(e){}$('gridView').classList.toggle('selected',view==='grid');$('gridView').setAttribute('aria-pressed',view==='grid');$('tableView').classList.toggle('selected',view==='table');$('tableView').setAttribute('aria-pressed',view==='table');if(ui.products)renderProducts();}
$('gridView').onclick=()=>setView('grid');$('tableView').onclick=()=>setView('table');
$('importNext').onclick=nextImport;$('importBack').onclick=importBack;
$('settingsForm').onsubmit=saveSettings;$('settingsForm').addEventListener('input',settingsExample);
$('folderForm').onsubmit=saveFolder;$('folderScan').onclick=scanFolder;
$('restoreInput').onchange=e=>{restoreFile=e.target.files[0] || null;$('restoreDetails').hidden=!restoreFile;$('restoreName').textContent=restoreFile?.name || '';$('restoreConfirm').checked=false;$('restoreButton').disabled=true;showError('backupError',null);};
$('restoreConfirm').onchange=e=>$('restoreButton').disabled=!e.target.checked;$('restoreButton').onclick=restoreBackup;
$('mobileFactory').onchange=e=>changeFilter('factory',e.target.value);
async function bootstrap(){try{await refreshState();await loadProducts();}catch(e){showError('catalogError',e);$('catalogBody').innerHTML='<div class="no-results">' + icon('folder') + '<h3>Не удалось открыть каталог</h3><p>Запустите локальную программу и попробуйте ещё раз.</p><button class="button secondary" data-action="retry">Повторить</button></div>';$('catalogBody').setAttribute('aria-busy','false');}}
try{setView(localStorage.getItem('catalogView')==='table'?'table':'grid');}catch(e){}
const dimensionKeys=['dimensions','width_min','width_max','height_min','height_max','depth_min','depth_max','dimension_order','dimension_unit'];
function renderDimensionHelp(data) {
  const help=$('dimensionHelp'), summary=data?.dimension_filter;
  const form=$('dimensionPanel');
  const hasRange=dimensionKeys.slice(1,7).some(key=>form.elements[key].value.trim());
  const missingOrder=!form.elements.dimension_order.value, missingUnit=!form.elements.dimension_unit.value;
  let text='';
  if(hasRange && (missingOrder || missingUnit)) text='Для размеров без подписей выберите ' + [missingOrder?'порядок Ш / В / Г':'',missingUnit?'единицы прайса':''].filter(Boolean).join(' и ') + '. Можно подтвердить формат Ш × В × Г, миллиметры кнопкой ниже.';
  if(summary?.unrecognized) {
    const names={width:'ширину',height:'высоту',depth:'глубину'};
    const axes=summary.axes.map(axis=>names[axis]).join(', ');
    text+=(text?' ':'')+'Не проверены товары: '+number(summary.unrecognized)+'. Не удалось определить '+axes+' в миллиметрах.';
    if(summary.examples.length) text+=' Примеры записи: '+summary.examples.join(' · ')+'.';
  }
  help.textContent=text; help.hidden=!text;
}
function applyDimensions() {
  for(const key of dimensionKeys)ui.filters[key]=$('dimensionPanel').elements[key].value.trim();
  ui.page=1;$('dimensionToggle').textContent=dimensionKeys.slice(0,7).some(k=>ui.filters[k])?'Размеры · фильтр':'Размеры';
  renderDimensionHelp(null);loadProducts();
}
$('dimensionPanel').addEventListener('input',()=>renderDimensionHelp(null));
$('dimensionPanel').addEventListener('change',()=>renderDimensionHelp(null));
$('dimensionMillimeters').onclick=()=>{
  const form=$('dimensionPanel');form.elements.dimension_order.value='whd';form.elements.dimension_unit.value='mm';applyDimensions();
};
$('dimensionToggle').onclick=()=>{const panel=$('dimensionPanel');panel.hidden=!panel.hidden;$('dimensionToggle').setAttribute('aria-expanded',String(!panel.hidden));if(!panel.hidden)panel.elements.dimensions.focus();};
$('dimensionPanel').onsubmit=event=>{event.preventDefault();applyDimensions();};
$('clearDimensions').onclick=()=>{$('dimensionPanel').reset();for(const key of dimensionKeys)ui.filters[key]='';$('dimensionToggle').textContent='Размеры';ui.page=1;loadProducts();};
let updateTimer=null,updateInstalling=false;
function renderUpdate(value){
  $('updateCurrentVersion').textContent=value.version;
  $('updateStatus').textContent=!value.source && value.status==='idle' ? 'Источник ещё не подключён. Для обновления через интернет нужен постоянный адрес новых версий.' : (value.message || 'Укажите источник обновлений, чтобы проверить новую версию.');
  $('updateNotes').textContent=value.notes || '';
  const busy=['checking','preparing','installing'].includes(value.status);
  $('checkUpdate').disabled=busy;
  $('updateSource').disabled=busy;
  $('installUpdate').disabled=busy || !value.available || value.source!==$('updateSource').value.trim();
  if(value.status==='error')showError('updateError',new Error(value.message));
  if(value.last_result?.status==='error')showError('updateError',new Error(value.last_result.message));
  if(updateInstalling && value.version!==ui.state.version){location.reload();return;}
  if(updateInstalling && ['error','current'].includes(value.status))updateInstalling=false;
}
async function openUpdate(){
  showError('updateError',null);showDialog('updateDialog');
  try{const value=await api('/api/update');$('updateSource').value=value.source || '';renderUpdate(value);if(!value.source)$('updateStatus').textContent='Источник ещё не подключён. Для обновления через интернет нужен постоянный адрес новых версий.';pollUpdate();}
  catch(e){showError('updateError',e);}
}
function pollUpdate(){
  clearTimeout(updateTimer);
  if(!$('updateDialog').open)return;
  updateTimer=setTimeout(async()=>{try{renderUpdate(await api('/api/update'));}catch(e){if(updateInstalling)$('updateStatus').textContent='Каталог перезапускается. Ожидаем подключения…';else showError('updateError',e);}pollUpdate();},1500);
}
async function requestUpdate(install){
  showError('updateError',null);
  const button=$(install?'installUpdate':'checkUpdate');loading(button,true);
  try{await api('/api/update/'+(install?'install':'check'),{source:$('updateSource').value.trim()});updateInstalling=install;$('updateStatus').textContent=install?'Загружаем и проверяем обновление…':'Проверяем новую версию…';$('checkUpdate').disabled=true;$('installUpdate').disabled=true;pollUpdate();}
  catch(e){showError('updateError',e);}
  finally{loading(button,false);if(!$('updateError').hidden)return;$('checkUpdate').disabled=true;$('installUpdate').disabled=true;}
}
$('checkUpdate').onclick=()=>requestUpdate(false);$('installUpdate').onclick=()=>requestUpdate(true);
$('updateSource').oninput=()=>{$('installUpdate').disabled=true;};
bootstrap();
setInterval(async()=>{if(!ui.state || document.querySelector('dialog[open]'))return;try{const previous=ui.state.revision;const previousImport=ui.state.stats.last_import;ui.state=await api('/api/state');renderState();if((previous!==undefined && previous!==ui.state.revision) || previousImport!==ui.state.stats.last_import)await loadProducts();}catch(e){}},20000);
