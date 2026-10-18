'use strict';
// Local-only matching UI, sharing the catalog's api(), esc(), money() and date().
(() => {
  const view={page:1,detail:null,selected:null,busy:false,request:0,queue:'suggested',items:[]};
  const states={conflict:'Проверить товар',price_review:'Проверить изменение цены',price_unknown:'Проверить цену на Авито',unchanged:'Цена совпадает',blocked_transport:'Изменение подготовлено'};
  const decisions={confirm:'Подтверждено',reject:'Кандидат отклонён',unlink:'Связь отменена',leave:'Без соответствия',exclude:'Исключено из синхронизации',restore:'Возвращено на проверку'};
  const plans={prepared:'Подготовлен локально',stale:'Устарел',cancelled:'Отменён'};
  const productKeys=['id','name','factory','collection','sku','color','dimensions','variant'];
  const attributes={factory:'Фабрика, если достоверно известна',collection:'Коллекция',sku:'Артикул',color:'Цвет',dimensions:'Размеры (с единицами и порядком, если известны)',material:'Материал',variant:'Комплектация / вариант'};
  const fields={avito_id:'Номер существующего объявления',title:'Название',category:'Категория',status:'Статус как в источнике',price:'Последняя известная цена, ₽',price_source:'Источник цены',url:'HTTPS-ссылка Авито'};
  const cents=v=>v===null || v===undefined?'Неизвестна':money(v/100);
  const snapshot=p=>Object.fromEntries(productKeys.map(k=>[k,p[k] ?? '']));
  const fail=e=>showError('avitoError',e);
  const field=(key,label)=>'<label class="field">'+esc(label)+'<input name="'+key+'" '+(['avito_id','title','price_source'].includes(key)?'required ':'')+(key==='price'?'type="number" min="0.01" step="0.01"':key==='url'?'type="url"':'maxlength="3000"')+'></label>';
  $('avitoSection').innerHTML='<div class="catalog-heading"><h2 id="avitoHeading">Авито · сопоставление</h2><span class="count-pill">Локальный этап А</span></div><div class="notice">Отправка на Авито заблокирована. Здесь сохраняются локальные сведения, подтверждённые связи и тестовые планы.</div><div id="avitoError" class="notice error" hidden role="alert"></div><div id="avitoSetup" hidden><p>Перед созданием таблиц будет сохранена резервная копия всей базы и фотографий.</p><button id="avitoSetupButton" class="button primary">Создать резервную копию и подготовить раздел</button></div><div id="avitoWorkspace" hidden><div class="avito-toolbar"><label class="field">Фабрика для кандидатов и плана<select id="avitoFactory"></select></label><label class="field">Поиск объявлений<input id="avitoSearch" type="search" placeholder="ID или название"></label><label class="field">Показать<select id="avitoFilter"><option value="">Все записи</option><option value="unlinked">Без связи</option><option value="linked">Со связью</option><option value="demo">Только тестовые</option></select></label></div><p class="muted small-text">Несопоставленные объявления показаны независимо от фабрики: их производитель ещё не установлен. Фабрика ограничивает кандидатов и тестовый план.</p><div class="avito-actions"><button id="avitoManual" class="button secondary">Добавить сведения вручную</button><button id="avitoDemo" class="button secondary">Добавить тестовые объявления</button><button id="avitoPrepare" class="button primary">Тестовое сравнение цен</button><button id="avitoJournalButton" class="button secondary">Журнал</button></div><p class="muted small-text">Импорт реальной выгрузки будет подключён после проверки вашего файла. Записи DEMO — синтетические, не объявления Авито.</p><form id="avitoManualForm" class="avito-card" hidden><h3>Локальные сведения об объявлении</h3><p>Форма не изменяет объявление на Авито. Неизвестные поля оставьте пустыми.</p><div class="form-grid">'+Object.entries(fields).map(([k,l])=>field(k,l)).join('')+'<label class="field">Когда цена наблюдалась<input name="observed_local" type="datetime-local"></label><label class="field full">Описание<textarea name="description" rows="3" maxlength="30000"></textarea></label>'+Object.entries(attributes).map(([k,l])=>field(k,l)).join('')+'</div><div class="avito-actions"><button class="button primary" type="submit">Сохранить локально</button><button id="avitoManualCancel" class="button secondary" type="button">Закрыть</button></div></form><div id="avitoListings"></div><div id="avitoPagination" class="avito-actions"></div><div id="avitoDetail" class="avito-card" hidden></div><div id="avitoPlan" class="avito-card" hidden></div><div id="avitoHistory" class="avito-card" hidden></div></div>';
  function close(){
    view.detailRequest=(view.detailRequest || 0)+1;view.request++;
    $('avitoSection').hidden=true;document.querySelector('.catalog-section').hidden=false;document.querySelector('.overview').hidden=false;
    document.querySelector('.page-header').hidden=false;
    $('navAvito').classList.remove('active');$('navCatalog').classList.add('active');
  }
  // An export is parsed into a frozen preview, then committed only after the
  // user selects categories. No external URLs in the workbook are fetched.
  $('avitoDemo').insertAdjacentHTML('beforebegin','<button class="button secondary" id="avitoImportButton">Импорт выгрузки XLSX</button>');
  $('avitoManualForm').insertAdjacentHTML('beforebegin','<form id="avitoImportForm" class="avito-card" hidden><h3>Импорт существующих объявлений</h3><label class="field">Файл выгрузки Авито<input id="avitoImportFile" type="file" accept=".xlsx" required></label><label class="field">Режим<select id="avitoImportMode"><option value="add">Добавить новые, существующие сохранить</option><option value="update">Обновить локальные сведения и добавить новые</option></select></label><label class="field">Время снимка, если достоверно известно<input id="avitoImportObserved" type="datetime-local"></label><p class="muted small-text">Оставьте время пустым, если неизвестно. Дата окончания объявления и дата загрузки файла не являются временем наблюдения цены. Фотографии не скачиваются.</p><button class="button primary" id="avitoInspectButton" type="submit">Предпросмотр</button><div id="avitoImportPreview"></div></form>');
  $('avitoImportButton').onclick=()=>{$('avitoImportForm').hidden=!$('avitoImportForm').hidden;};
  for(const id of ['avitoImportFile','avitoImportMode','avitoImportObserved'])$(id).onchange=()=>{view.importSession=null;$('avitoImportPreview').innerHTML='';};
  $('avitoImportForm').onsubmit=e=>{e.preventDefault();act($('avitoInspectButton'),async()=>{
    const file=$('avitoImportFile').files[0];if(!file)throw new Error('Выберите выгрузку XLSX.');
    const data=await readFile(file);data.mode=$('avitoImportMode').value;
    data.observed_at=$('avitoImportObserved').value?new Date($('avitoImportObserved').value).toISOString():null;
    const result=await api('/api/avito/import/inspect',data);view.importSession=result.session_id;
    $('avitoImportPreview').innerHTML='<h4>Найдено '+result.total+' объявлений</h4><p>Новых: '+result.new+', существующих: '+result.existing+', проблем: '+result.issues_count+'.</p><p>'+esc(result.notice)+'</p><div>'+Object.entries(result.categories).map(([c,n])=>'<label class="check-label"><input type="checkbox" name="avitoImportCategory" value="'+esc(c)+'" '+(c==='Мебель и интерьер'?'checked':'')+' '+(!['Мебель и интерьер','Товары для детей и игрушки'].includes(c)?'disabled':'')+'><span>'+esc(c || 'Категория неизвестна')+' · '+n+'</span></label>').join('')+'</div><details><summary>Первые '+result.sample.length+' записей</summary><div class="table-container"><table class="product-table"><thead><tr><th>Номер Авито</th><th>Название</th><th>Категория</th><th>Цена снимка</th><th>Проверка</th></tr></thead><tbody>'+result.sample.map(i=>'<tr><td>'+esc(i.avito_id)+'</td><td>'+esc(i.title)+'</td><td>'+esc(i.category)+'</td><td>'+money(i.price)+'</td><td>'+esc(i.import_state)+'</td></tr>').join('')+'</tbody></table></div></details>'+result.issues.map(i=>'<p class="avito-conflict">'+esc(i.sheet || '')+' '+esc(i.row || '')+' '+esc(i.avito_id || '')+': '+esc(i.message)+'</p>').join('')+'<button class="button primary" id="avitoCommitImport" type="button">Сохранить выбранные категории локально</button>';
    $('avitoCommitImport').onclick=ev=>act(ev.target,async()=>{
      const categories=Array.from(document.querySelectorAll('input[name="avitoImportCategory"]:checked')).map(i=>i.value);
      const r=await api('/api/avito/import/commit',{session_id:view.importSession,categories});
      $('avitoImportPreview').innerHTML='<p class="notice">Добавлено '+r.added+', обновлено '+r.updated+', сохранено без изменений / пропущено '+r.skipped+'. На Авито ничего не отправлено.</p>';
      view.page=1;await listings();await refreshPlans();
    });
  });};
  // Replace the initial waiting-for-file note now that the supplied XLSX layout
  // is supported; the synthetic demo remains separately labelled.
  const note=Array.from($('avitoWorkspace').querySelectorAll('p')).find(p=>p.textContent.startsWith('Импорт реальной выгрузки'));
  if(note)note.textContent='Поддерживается предоставленная выгрузка XLSX: листы категорий и русские/английские заголовки. Записи DEMO синтетические. Импорт не подтверждает соответствия автоматически.';
  const queueLabels={suggested:'Нужно связать',linked:'Уже связаны',unassigned:'Фабрика не определена',excluded:'Комплекты и исключённые'};
  $('avitoHeading').textContent='Авито · подготовка цен';
  const workspace=$('avitoWorkspace'),toolbar=workspace.querySelector('.avito-toolbar');
  toolbar.firstElementChild.firstChild.textContent='1. Выберите фабрику';
  $('avitoSearch').placeholder='Название, коллекция или номер объявления';
  workspace.querySelectorAll(':scope > .muted').forEach(p=>p.hidden=true);
  workspace.insertAdjacentHTML('afterbegin','<div class="avito-flow"><div><b>1</b><strong>Сопоставить одиночные товары</strong><span>Найти объявление → проверить → сохранить связь</span></div><div><b>2</b><strong>Сравнить цены</strong><span>Готовая цена каталога и последняя известная цена Авито</span></div><div class="avito-flow-later"><b>3</b><strong>Отправить на Авито — позже</strong><span>После проверки официального способа и отдельного разрешения</span></div></div><details class="avito-how"><summary>Как цены попадут на Авито?</summary><p>Сейчас здесь только локальная подготовка. Вы один раз подтверждаете связь одиночного товара с существующим ID объявления. После обновления прайса приложение сможет сравнить готовую розничную цену каталога с известной ценой объявления.</p><p>Официальный способ изменения ручных объявлений ещё не подтверждён. Сейчас файл для загрузки на Авито не создаётся, отправка отсутствует. Когда способ будет проверен, отдельный шаг покажет точный список изменений для пилота 5–10 объявлений. Комплекты исключены.</p></details>');
  const actions=$('avitoPrepare').parentElement;
  const tools=document.createElement('details');tools.id='avitoTools';tools.className='avito-tools';tools.innerHTML='<summary>Выгрузка и инструменты</summary>';
  for(const id of ['avitoImportButton','avitoManual','avitoDemo','avitoJournalButton'])tools.append($(id));
  tools.append($('avitoFilter').closest('label'));
  for(const id of ['avitoImportForm','avitoManualForm','avitoHistory'])tools.append($(id));
  workspace.append(tools);
  actions.innerHTML='<button id="avitoReviewStep" class="button secondary">1. Сопоставление</button><button id="avitoPrepare" class="button primary">2. Сравнить цены выбранной фабрики</button>';
  const panel=document.createElement('div');panel.id='avitoQueuePanel';
  toolbar.after(actions,panel);
  panel.insertAdjacentHTML('afterbegin','<div id="avitoQueues" class="avito-queues"></div><p id="avitoQueueHint" class="avito-queue-hint"></p><div class="avito-narrow"><label class="field">Тип мебели<select id="avitoType"><option value="">Любой тип мебели</option></select></label><label class="field">Коллекция<select id="avitoCollection"><option value="">Любая коллекция</option></select></label><button class="text-button" id="avitoReset">Сбросить поиск и фильтры</button></div>');
  panel.append($('avitoListings'),$('avitoPagination'));
  $('avitoReviewStep').onclick=()=>{view.mode='matching';view.detail=null;$('avitoPlan').hidden=true;$('avitoDetail').hidden=true;panel.hidden=false;};
  $('avitoReset').onclick=()=>{$('avitoSearch').value='';$('avitoType').value='';$('avitoCollection').value='';$('avitoFilter').value='';view.page=1;listings().catch(fail);};
  for(const id of ['avitoType','avitoCollection'])$(id).onchange=()=>{view.page=1;listings().catch(fail);};
  // Each screen has one purpose; the existing local APIs stay unchanged.
  $('avitoSection').classList.add('avito-workspace');
  $('avitoHeading').textContent='Авито · обновление цен';
  $('avitoSection').querySelector('.count-pill').textContent='Подготовка на компьютере';
  const banner=$('avitoSection').querySelector(':scope > .notice');
  banner.classList.add('av2-local-banner');banner.textContent='На Авито ничего не отправляется. Здесь вы связываете товары и проверяете цены.';
  workspace.querySelector('.avito-flow').remove();workspace.querySelector('.avito-how').remove();
  const steps=document.createElement('nav');steps.className='av2-steps';steps.setAttribute('aria-label','Этапы работы с Авито');
  steps.innerHTML='<button data-avito-step="matching"><b>1</b><span><strong>Связать товары</strong><small>Один раз для каждого объявления</small></span></button><button data-avito-step="prices"><b>2</b><span><strong>Проверить цены</strong><small>После обновления прайса</small></span></button><button data-avito-step="transfer"><b>3</b><span><strong>Отправка</strong><small>Как подключить передачу цен</small></span></button>';
  toolbar.before(steps);
  toolbar.firstElementChild.firstChild.textContent='Фабрика';
  const search=$('avitoSearch').closest('label');search.firstChild.textContent='Найти объявление';
  const searchRow=document.createElement('div');searchRow.className='av2-search-row';
  const filters=document.createElement('details');filters.className='av2-filters';filters.innerHTML='<summary>Фильтры</summary>';
  filters.append(workspace.querySelector('.avito-narrow'),$('avitoFilter').closest('label'));
  searchRow.append(search,filters);$('avitoQueueHint').after(searchRow);
  toolbar.insertAdjacentHTML('beforeend','<p class="av2-scope-hint">Работаем с одной фабрикой. Объявления без установленного производителя вынесены в отдельную группу.</p>');
  actions.hidden=true;
  const priceScreen=document.createElement('section');priceScreen.id='avitoPricesScreen';priceScreen.hidden=true;
  priceScreen.innerHTML='<div class="av2-screen-head"><div><h3>Проверьте новые цены</h3><p>Только сохранённые связи выбранной фабрики, без комплектов. Используется готовая розничная цена каталога.</p></div></div><div class="av2-callout"><strong>Это локальное сравнение</strong><p>Цена из выгрузки могла устареть. Сохранение сравнения не изменяет объявления на Авито.</p></div>';
  workspace.append(priceScreen);
  priceScreen.querySelector('.av2-screen-head').append($('avitoPrepare'));
  $('avitoPrepare').textContent='Сравнить цены';priceScreen.append($('avitoPlan'));workspace.append(priceScreen);
  const transfer=document.createElement('section');transfer.id='avitoTransferScreen';transfer.hidden=true;
  transfer.innerHTML=`<div class="av2-transfer-intro"><span class="av2-status">ПОКА НЕ ПОДКЛЮЧЕНО</span><h3>Как цены попадут на Авито</h3><p>Связи и сравнения готовы для локальной работы. Передача изменений в ваш аккаунт — следующий отдельный этап.</p></div>
    <div class="av2-transfer-grid"><article class="avito-card"><span class="av2-eyebrow">УЖЕ РАБОТАЕТ</span><h4>Подготовить цены</h4><ol><li>Загрузить список существующих объявлений.</li><li>Один раз связать объявления с товарами каталога.</li><li>Загрузить новый прайс фабрики обычным способом.</li><li>Проверить новые розничные цены на шаге 2.</li></ol><button class="button secondary" data-avito-step="prices">Перейти к проверке цен</button></article>
    <article class="avito-card av2-future"><span class="av2-eyebrow">НУЖНО ПОДКЛЮЧИТЬ</span><h4>Передать изменения официально</h4><ol><li>Подтвердить способ изменения ручных объявлений для вашего аккаунта.</li><li>Согласовать обновление 5–10 существующих объявлений.</li><li>Передать цены и сверить результат на Авито.</li><li>После успешного пилота включить обновления по прайсам.</li></ol></article></div>
    <div class="av2-callout"><strong>Выгрузку из Авито не нужно загружать обратно</strong><p>Ваш Excel — входной список для сопоставления. Сейчас приложение не создаёт файл для автозагрузки и не отправляет изменения в аккаунт.</p></div>
    <details class="av2-faq"><summary>Что нужно проверить перед подключением</summary><p>Доступен ли в вашем аккаунте официальный API изменения цены ручного объявления или сначала нужен перевод на управление автозагрузкой. Возможность пока не подтверждена: конкретный метод API и формат файла не выбраны.</p><p>Затем проверим идентификаторы, отсутствие дубликатов, сохранение описаний и фотографий, поведение остальных объявлений и способ получения фактической цены. После этого можно реализовать передачу, журнал результатов, ограничения и остановку.</p></details>`;
  workspace.append(transfer);
  const auxiliary=document.createElement('section');auxiliary.id='avitoAuxiliary';auxiliary.hidden=true;
  auxiliary.innerHTML='<button id="avitoBackToWork" class="text-button">← Вернуться к объявлениям</button>';
  auxiliary.append($('avitoImportForm'),$('avitoManualForm'),$('avitoHistory'));workspace.append(auxiliary);
  tools.querySelector('summary').textContent='Ещё';tools.classList.add('av2-tools');
  const toolMenu=document.createElement('div');toolMenu.className='av2-tools-menu';
  Array.from(tools.querySelectorAll(':scope > button')).forEach(b=>toolMenu.append(b));tools.append(toolMenu);
  const filterPanel=document.createElement('div');filterPanel.className='av2-filter-panel';
  Array.from(filters.children).filter(el=>el.tagName!=='SUMMARY').forEach(el=>filterPanel.append(el));filters.append(filterPanel);
  const header=$('avitoSection').querySelector('.catalog-heading');
  header.classList.add('av2-header');header.append($('avitoImportButton'),tools);
  $('avitoImportButton').textContent='Загрузить список из Авито';
  $('avitoImportForm').querySelector('h3').textContent='Загрузка списка из Авито';
  $('avitoImportForm').querySelector('h3').insertAdjacentHTML('afterend','<p>Этот файл нужен, чтобы прочитать ваши существующие объявления и связать их с каталогом. Сведения сохраняются только на компьютере; обратной отправки в Авито здесь нет.</p>');
  $('avitoManual').textContent='Добавить запись вручную';$('avitoDemo').textContent='Добавить тестовые записи DEMO';
  header.insertAdjacentHTML('afterend','<p id="avitoProgress" class="av2-progress"></p>');
  panel.insertAdjacentHTML('afterbegin','<div class="av2-screen-head"><div><h3>Выберите объявление и товар каталога</h3><p>После сохранения связи повторять сопоставление при каждом новом прайсе не нужно.</p></div></div>');
  $('avitoImportButton').onclick=()=>showScreen('import');
  $('avitoBackToWork').onclick=()=>showScreen('matching');
  function showScreen(mode){
    view.mode=mode;view.detailRequest=(view.detailRequest || 0)+1;
    const extra=['import','manual','journal'].includes(mode);
    toolbar.hidden=mode==='transfer' || extra;panel.hidden=mode!=='matching';$('avitoDetail').hidden=true;
    priceScreen.hidden=mode!=='prices';$('avitoPlan').hidden=mode!=='prices';transfer.hidden=mode!=='transfer';auxiliary.hidden=!extra;
    $('avitoImportForm').hidden=mode!=='import';$('avitoManualForm').hidden=mode!=='manual';$('avitoHistory').hidden=mode!=='journal';
    steps.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.avitoStep===mode)));
    tools.open=false;
    $('avitoHeading').scrollIntoView({block:'start'});
    if(mode==='prices'){
      $('avitoPrepare').disabled=!$('avitoFactory').value;
      if(!$('avitoPlanDetails')?.textContent)$('avitoPlanDetails')?.insertAdjacentHTML('afterbegin','<div class="av2-empty"><h4>Сравнение ещё не открыто</h4><p>Нажмите «Сравнить цены» или откройте сохранённое сравнение ниже.</p></div>');
    }
  }
  async function updateProgress(){
    const data=await api('/api/avito/state');
    $('avitoProgress').textContent=data.installed?`${data.listings} объявлений из выгрузки · ${data.links} сохранённых связей`:'';
    $('avitoImportButton').disabled=!data.installed;tools.hidden=!data.installed;
    return data;
  }
  async function open(){
    $('avitoSection').hidden=false;document.querySelector('.catalog-section').hidden=true;document.querySelector('.overview').hidden=true;
    document.querySelector('.page-header').hidden=true;
    $('navAvito').classList.add('active');$('navCatalog').classList.remove('active');fail(null);
    try{
      await refreshState();
      let remembered=null;try{remembered=sessionStorage.getItem('avito.factory');}catch{}
      const chosen=$('avitoFactory').options.length?$('avitoFactory').value:remembered,fs=ui.state.factories;
      $('avitoFactory').innerHTML='<option value="">Все фабрики — только поиск</option>'+fs.map(f=>'<option value="'+esc(f.name)+'">'+esc(f.name)+'</option>').join('');
      $('avitoFactory').value=chosen===''?'':fs.some(f=>f.name===chosen)?chosen:fs.find(f=>f.name==='Регион 058')?.name || '';
      const data=await updateProgress();$('avitoSetup').hidden=data.installed;$('avitoWorkspace').hidden=!data.installed;
      if(chosen===null){const recent=data.plans?.find(p=>fs.some(f=>f.name===p.factory));if(recent)$('avitoFactory').value=recent.factory;}
      if(data.installed){await listings();renderPlans(data.plans);showScreen(['matching','prices','transfer'].includes(view.mode)?view.mode:'matching');}
    }catch(e){fail(e.status===404?new Error('Запущена старая версия сервера без раздела Авито. Остановите каталог через Stop.ps1, затем запустите Start.cmd и обновите страницу.'):e);}
  }
  async function act(button,work){
    if(view.busy)return;view.busy=true;fail(null);loading(button,true);
    $('avitoFactory').disabled=true;
    try{await work();}catch(e){fail(e);}finally{view.busy=false;loading(button,false);$('avitoFactory').disabled=false;}
  }
  async function listings(){
    const request=++view.request;
    $('avitoListings').innerHTML='<p class="avito-empty" role="status">Отбираем объявления по фабрике и признакам товара…</p>';
    const data=await api('/api/avito/listings?'+new URLSearchParams({factory:$('avitoFactory').value,q:$('avitoSearch').value,filter:$('avitoFilter').value,page:view.page,queue:view.queue,type:$('avitoType').value,collection:$('avitoCollection').value}));
    if(request!==view.request)return;view.page=data.page;
    view.items=data.items;
    for(const [id,values,label] of [['avitoType',data.types,'Любой тип мебели'],['avitoCollection',data.collections,'Любая коллекция']]){
      const value=$(id).value;$(id).innerHTML='<option value="">'+label+'</option>'+values.map(v=>'<option value="'+esc(v)+'">'+esc(v)+'</option>').join('');$(id).value=values.includes(value)?value:'';
    }
    $('avitoQueues').innerHTML=Object.entries(queueLabels).map(([key,label])=>'<button class="avito-queue '+(view.queue===key?'active':'')+'" data-avito-queue="'+key+'">'+esc(label)+' <b>'+data.counts[key]+'</b></button>').join('');
    $('avitoQueueHint').textContent=view.queue==='unassigned'?'Здесь производитель не установлен. Выбранная фабрика ограничивает только товары для ручного поиска. Объявления из этого списка не считаются её продукцией.':view.queue==='excluded'?'Комплекты, кухни, предложения под ключ и исключённые вручную объявления не получают цену отдельного товара и не входят в новое сравнение цен.':view.queue==='linked'?'Сохранённые вручную связи выбранной фабрики. Цены можно сравнить на следующем шаге.':'Только одиночные объявления с признаками выбранной фабрики. Подсказки по коллекции требуют проверки; автоматически связи не создаются.';
    $('avitoListings').innerHTML=data.items.length?'<div class="avito-review-list">'+data.items.map(a=>'<article class="avito-list-row"><div><small>'+esc(a.avito_id)+(a.is_demo?' · ТЕСТ':'')+' · '+esc(a.review.item_type?.label || 'Тип не определён')+'</small><h3>'+esc(a.title)+'</h3><p>'+esc(a.review.excluded_reason || (a.product_name?'Связь: '+a.product_name:a.review.factory_basis))+'</p><small>'+esc(a.linked_factory || a.review.suggested_factory || 'Фабрика неизвестна')+(a.review.collections.length?' · '+a.review.collections.map(esc).join(', '):'')+'</small></div><div class="avito-row-action"><strong>'+money(a.price)+'</strong><small>Цена из выгрузки</small><button class="button secondary" data-avito-open="'+esc(a.id)+'">'+(a.product_name?'Посмотреть связь':a.review.excluded_reason?'Посмотреть':'Выбрать товар')+'</button></div></article>').join('')+'</div>':'<div class="avito-empty"><h3>В этой группе объявлений нет</h3><p>'+(view.queue==='suggested'?'Проверьте поиск и фильтры. Если фабрика не указана в объявлении и серия не позволяет её выделить, ищите в группе «Фабрика неизвестна». Импорт доступен в «Выгрузка и инструменты».':'Попробуйте сбросить поиск или выбрать другую группу.')+'</p></div>';
    $('avitoPagination').innerHTML='<span>'+data.total+' записей · '+data.page+' / '+data.pages+'</span><button class="button small secondary" data-avito-page="'+(data.page-1)+'" '+(data.page<=1?'disabled':'')+'>Назад</button><button class="button small secondary" data-avito-page="'+(data.page+1)+'" '+(data.page>=data.pages?'disabled':'')+'>Далее</button>';
  }
  function dimensionTable(check,confirmed=false){
    const rows=check.dimension_check?.rows || [];
    return (rows.length?'<div class="table-container"><table class="avito-dimension-table"><thead><tr><th>Габарит</th><th>Объявление, мм</th><th>Каталог</th><th>Разница, мм</th></tr></thead><tbody>'+rows.map(r=>'<tr><td>'+esc(r.label)+'</td><td>'+esc(r.listing_mm ?? '—')+'</td><td>'+esc(r.catalog_mm!==null?r.catalog_mm+' мм':r.catalog_raw?r.catalog_raw+' (ед. не указаны)':'—')+'</td><td>'+esc(r.difference_mm ?? '—')+'</td></tr>').join('')+'</tbody></table></div>':'')+(check.warnings?.length?'<details class="avito-warning"><summary><strong>'+(confirmed&&!check.conflicts.length?'Расхождения проверены пользователем':'Предупреждения для ручной проверки')+' · '+check.warnings.length+'</strong></summary><ul>'+check.warnings.map(w=>'<li>'+esc(w)+'</li>').join('')+'</ul></details>':'');
  }
  function candidate(c,confirmed=false){
    const p=c.product,k=c.check;
    return '<article class="avito-candidate">'+(p.photo_url?'<img src="'+esc(p.photo_url)+'" alt="Фото товара каталога" loading="lazy">':'<div class="avito-no-photo">Нет фото каталога</div>')+'<div><h4>'+esc(p.name)+'</h4><p>'+[p.factory,p.collection,p.sku,p.dimensions,p.color,p.variant].filter(Boolean).map(esc).join(' · ')+'</p><strong>'+money(p.sale_price)+'</strong>'+dimensionTable(k,confirmed)+'<ul>'+k.reasons.map(r=>'<li>'+esc(r)+'</li>').join('')+'</ul>'+(k.conflicts.length?'<p class="avito-conflict">'+k.conflicts.map(esc).join('<br>')+'</p>':'')+'<p class="muted small-text">Неизвестно в объявлении: '+k.missing.map(esc).join(', ')+'</p>'+(confirmed?'':'<div class="avito-actions"><button class="button small primary" data-avito-choose="'+esc(p.id)+'" '+(k.conflicts.length?'disabled':'')+'>Выбрать для подтверждения</button><button class="button small secondary" data-avito-reject="'+esc(p.id)+'">Отклонить</button></div>')+'</div></article>';
  }
  async function detail(id,q='',unit){
    showScreen('matching');
    const request=++view.detailRequest;
    panel.hidden=true;$('avitoDetail').hidden=false;$('avitoDetail').innerHTML='<p class="avito-empty" role="status">Открываем объявление…</p>';
    const params={factory:$('avitoFactory').value,q};if(unit!==undefined)params.dimension_unit=unit;
    const d=await api('/api/avito/listings/'+id+'?'+new URLSearchParams(params));
    if(request!==view.detailRequest)return;view.detail=d;view.selected=null;
    const a=d.listing;$('avitoDetail').hidden=false;
    $('avitoDetail').innerHTML='<h3>'+esc(a.avito_id)+' · '+esc(d.label)+'</h3>'+(a.is_demo?'<p class="avito-demo">ТЕСТОВЫЕ ДАННЫЕ — не реальное объявление</p>':'')+'<h4>'+esc(a.title)+'</h4><p class="avito-description">'+esc(a.description || 'Описание отсутствует')+'</p><p>'+esc(a.category)+' · '+esc(a.status || 'Статус неизвестен')+'</p><p>Последняя известная цена: <strong>'+money(a.price)+'</strong> · '+esc(a.price_source)+' · '+esc(a.observed_at?date(a.observed_at,true):'Время неизвестно')+'</p><p class="muted small-text">Фотографии объявления пока не импортируются. Сравнение изображений — этап Б.</p>'+(!a.is_demo?'<button class="button small secondary" id="avitoEditLocal">Дополнить локальные сведения</button>':'')+(d.confirmed?'<h4>Сохранённая связь</h4>'+candidate(d.confirmed,true)+'<button class="button small secondary" data-avito-decision="unlink">Отменить связь</button>':'')+'<form id="avitoCandidateSearch" class="avito-actions"><label class="field">Поиск товара в каталоге<input name="q" value="'+esc(q)+'" placeholder="Название или артикул"></label><button class="button secondary">Найти</button></form><h4>Кандидаты ('+d.candidate_count+', показано до 10)</h4>'+d.candidates.map(c=>candidate(c)).join('')+(!d.candidates.length?'<p>Кандидаты не найдены. Попробуйте поиск или оставьте запись без соответствия.</p>':'')+'<div id="avitoConfirmArea" hidden></div><div class="avito-actions"><button class="button secondary" data-avito-decision="leave">Оставить без соответствия</button></div><details><summary>История решений</summary>'+d.history.map(h=>'<p>'+esc(date(h.created_at,true))+' · '+esc(decisions[h.action] || h.action)+' · '+esc(h.reason)+'</p>').join('')+'</details>';
    $('avitoCandidateSearch').onsubmit=e=>{e.preventDefault();detail(id,e.target.elements.q.value,$('avitoDimensionUnit').value).catch(fail);};
    const context=d.match_context || {}, collections=context.collections || [];
    const explanation=document.createElement('div');explanation.className='notice';
    explanation.innerHTML='<strong>Что распознано в объявлении</strong><br>Тип: '+esc(context.item_type?.label || 'Не определён')+'<br>Коллекция: '+(collections.length?collections.map(c=>'«'+esc(c.name)+'» ('+esc(c.source)+')').join(', '):'Не определена')+'<br><small>Исключено товаров другого типа: '+(d.excluded?.type || 0)+', другой коллекции: '+(d.excluded?.collection || 0)+'. Если подходящих нет, измените фабрику или проверьте описание. Связь по-прежнему подтверждается вручную.</small>';
    explanation.insertAdjacentHTML('beforeend','<br>Модель: '+esc(context.model?.name || 'Не определена')+(context.sleep_width_mm?'<br>Спальное место: ширина '+context.sleep_width_mm+' мм (из описания)':'')+'<br><small>Отсеяно других моделей: '+(d.excluded?.model || 0)+'.</small>');
    $('avitoDetail').insertBefore(explanation,$('avitoCandidateSearch'));
    $('avitoCandidateSearch').insertAdjacentHTML('beforebegin','<label class="field avito-dimension-unit">Единицы габаритов каталога для этой проверки<select id="avitoDimensionUnit"><option value="">Не указаны — не угадывать</option><option value="mm">Миллиметры</option><option value="cm">Сантиметры</option><option value="m">Метры</option></select><small>Габариты сравниваются по осям. Спальное место 140×200 и внешние размеры кровати — разные величины.</small></label>');
    $('avitoDimensionUnit').value=d.dimension_unit || '';
    $('avitoDimensionUnit').onchange=e=>detail(id,q,e.target.value).catch(fail);
    if(!d.candidates.length && !d.excluded_reason && context.model){
      $('avitoCandidateSearch').insertAdjacentHTML('afterend','<p class="notice">В описании указана модель «'+esc(context.model.name)+'». Совместимого товара этой модели в выбранной фабрике не найдено. Проверьте, есть ли она в каталоге; цена случайного похожего товара не подходит.</p>');
    }
    if(a.source_details && Object.keys(a.source_details).length){
      const info=document.createElement('details');
      info.innerHTML='<summary>Данные выгрузки и характеристики</summary><p>ID строки: '+esc(a.source_details.export_id || 'Не указан')+' · Лист: '+esc(a.source_details.sheet || '')+' · Строка: '+esc(a.source_details.row || '')+'</p><p>Ссылок на фотографии: '+(a.source_details.image_urls?.length || 0)+' (не загружались)</p><dl>'+Object.entries(a.source_details.characteristics || {}).map(([k,v])=>'<dt>'+esc(k)+'</dt><dd>'+esc(v)+'</dd>').join('')+'</dl><p class="muted small-text">Единицы и порядок размеров не угадываются. Фабрика не назначается по названию листа, бренду или выбранному фильтру.</p>';
      $('avitoDetail').insertBefore(info,$('avitoCandidateSearch'));
    }
    if($('avitoEditLocal'))$('avitoEditLocal').onclick=()=>manual(a);
    $('avitoQueuePanel').hidden=true;$('avitoPlan').hidden=true;$('avitoDetail').hidden=false;
    $('avitoDetail').insertAdjacentHTML('afterbegin','<div class="avito-detail-nav"><button class="button secondary" id="avitoBack">← К списку</button><span>Одна связь сохраняется один раз, затем используется при обновлении прайса</span><button class="button secondary" id="avitoNext">Следующее →</button></div>');
    $('avitoBack').onclick=()=>{view.detail=null;showScreen('matching');};
    const next=view.items[view.items.findIndex(i=>i.id===id)+1];$('avitoNext').disabled=!next;$('avitoNext').onclick=()=>detail(next.id).catch(fail);
    const desc=$('avitoDetail').querySelector('.avito-description');desc.textContent=a.description_text || a.description || 'Описание отсутствует';
    const description=document.createElement('details');description.innerHTML='<summary>Описание объявления</summary>';desc.before(description);description.append(desc);
    if(d.excluded_reason){
      $('avitoCandidateSearch').hidden=true;
      $('avitoDimensionUnit').closest('label').hidden=true;
      explanation.hidden=true;
      Array.from($('avitoDetail').children).filter(el=>(el.tagName==='H4' && el.textContent.startsWith('Кандидаты')) || (el.tagName==='P' && el.textContent.startsWith('Кандидаты не найдены'))).forEach(el=>el.hidden=true);
      $('avitoConfirmArea').insertAdjacentHTML('beforebegin','<div class="notice"><strong>Исключено из обновления цен</strong><p>'+esc(d.excluded_reason)+'. Состав комплекта и его цена будут отдельной задачей. Цена одного модуля не используется.</p>'+(!d.auto_excluded?'<button class="button secondary" data-avito-decision="restore">Вернуть одиночный товар на проверку</button>':'')+'</div>');
      $('avitoDetail').querySelectorAll('[data-avito-choose], [data-avito-decision="leave"]').forEach(b=>b.hidden=true);
    }else{
      $('avitoDetail').insertAdjacentHTML('beforeend','<button class="button secondary" data-avito-decision="exclude">Исключить: комплект / не обновлять цену</button>');
      if(d.confirmed){
        const start=$('avitoCandidateSearch'),end=$('avitoConfirmArea'),nodes=[];
        for(let el=start;el;el=el.nextSibling){nodes.push(el);if(el===end)break;}
        const change=document.createElement('details');change.innerHTML='<summary>Исправить связь: выбрать другой товар</summary>';start.before(change);change.append(...nodes);
      }
    }
    // Keep the ad beside the product selection, instead of stacking all data.
    const host=$('avitoDetail'),nav=host.querySelector('.avito-detail-nav');
    const layout=document.createElement('div');layout.className='av2-match-layout';
    const ad=document.createElement('aside');ad.className='av2-ad';
    const products=document.createElement('div');products.className='av2-products';
    const nodes=Array.from(host.children).filter(el=>el!==nav);
    let right=false;
    for(const el of nodes){
      if(el===explanation || (el.tagName==='H4' && el.textContent==='Сохранённая связь'))right=true;
      (right?products:ad).append(el);
    }
    layout.append(ad,products);host.append(layout);
    const recognized=document.createElement('details');recognized.className='av2-recognized';recognized.innerHTML='<summary>Что распознано в объявлении</summary>';
    recognized.hidden=!!d.excluded_reason;
    explanation.before(recognized);recognized.append(explanation);
    const unitLabel=$('avitoDimensionUnit').closest('label'),units=document.createElement('details');
    units.className='av2-recognized';units.innerHTML='<summary>Единицы габаритов каталога</summary>';
    unitLabel.before(units);units.append(unitLabel);if(d.excluded_reason)units.hidden=true;
    const source=Array.from(products.querySelectorAll('details')).find(el=>el.querySelector('summary')?.textContent==='Данные выгрузки и характеристики');
    if(source)ad.append(source);
    const cards=Array.from(host.querySelectorAll('[data-avito-choose]')).map(b=>b.closest('article'));
    cards.forEach((card,index)=>{
      if(index>=3)card.hidden=true;
      const body=card.lastElementChild,list=Array.from(body.children).find(el=>el.tagName==='UL'),missing=body.querySelector(':scope > .muted');
      if(list){const why=document.createElement('details');why.className='av2-evidence';why.innerHTML='<summary>Признаки совпадения</summary>';list.before(why);why.append(list);if(missing)why.append(missing);}
      const b=card.querySelector('[data-avito-choose]');b.textContent='Это нужный товар';
      if(b.disabled)b.title=d.candidates.find(c=>c.product.id===b.dataset.avitoChoose)?.check.conflicts.join('\n') || '';
    });
    if(cards.length>3){const more=document.createElement('button');more.id='avitoMoreCandidates';more.className='button secondary';more.textContent='Показать ещё '+(cards.length-3)+' вариантов';cards[2].after(more);more.onclick=()=>{cards.forEach(c=>c.hidden=false);more.remove();};}
    if(d.confirmed)products.insertAdjacentHTML('afterbegin','<div class="av2-saved"><strong>'+(d.confirmed.check.conflicts.length?'Связь требует проверки':'✓ Связь сохранена')+'</strong><p>После загрузки нового прайса переходите к проверке цен.</p><button class="button secondary" data-avito-step="prices">Проверить цены этой фабрики</button></div>');
    if(q && $('avitoCandidateSearch').closest('details'))$('avitoCandidateSearch').closest('details').open=true;
    $('avitoDetail').scrollIntoView({block:'start'});
  }
  function choose(id){
    const c=view.detail?.candidates.find(x=>x.product.id===id);if(!c || c.check.conflicts.length)return;view.selected=c.product;
    $('avitoConfirmArea').hidden=false;
    const button=Array.from($('avitoDetail').querySelectorAll('[data-avito-choose]')).find(b=>b.dataset.avitoChoose===id);
    button?.closest('article').after($('avitoConfirmArea'));
    $('avitoConfirmArea').innerHTML='<div class="notice">Товар «'+esc(c.product.name)+'», фабрика «'+esc(c.product.factory)+'». '+(view.detail.confirmed?'Предыдущая связь будет заменена с сохранением истории.':'')+'</div><label class="field">Основание ручного подтверждения<textarea id="avitoConfirmReason" maxlength="3000" rows="2" placeholder="Какие признаки проверены? Особенно важно при неизвестной фабрике."></textarea></label><button class="button primary" id="avitoConfirmButton">Подтвердить связь вручную</button>';
    $('avitoConfirmButton').textContent='Сохранить связь';
    $('avitoConfirmArea').insertAdjacentHTML('afterbegin','<button class="text-button" id="avitoChooseAgain">Выбрать другой товар</button>');
    $('avitoConfirmButton').insertAdjacentHTML('afterend','<button class="button secondary" id="avitoConfirmNext">Сохранить и открыть следующее</button>');
    const cards=Array.from($('avitoDetail').querySelectorAll('[data-avito-choose]')).map(b=>b.closest('article'));
    cards.forEach(card=>card.hidden=card!==button?.closest('article'));
    if($('avitoMoreCandidates'))$('avitoMoreCandidates').hidden=true;
    $('avitoChooseAgain').onclick=()=>{$('avitoConfirmArea').hidden=true;view.selected=null;cards.forEach(card=>card.hidden=false);};
    if(c.check.warnings?.length)$('avitoConfirmButton').insertAdjacentHTML('beforebegin','<div class="avito-warning"><strong>Нужна отдельная ручная проверка</strong><ul>'+c.check.warnings.map(w=>'<li>'+esc(w)+'</li>').join('')+'</ul><label class="check-label"><input id="avitoWarningAcknowledgment" type="checkbox"><span>Проверил: это тот же товар. Расхождения относятся к ошибкам или неполноте сведений. Основание указано выше.</span></label></div>');
    const finish=next=>{
      if(c.check.warnings?.length && !$('avitoWarningAcknowledgment').checked)throw new Error('Проверьте предупреждения и отметьте отдельное ручное подтверждение.');
      return decide('confirm',id,$('avitoConfirmReason').value,next);
    };
    $('avitoConfirmButton').onclick=e=>act(e.target,()=>finish(false));
    $('avitoConfirmNext').onclick=e=>act(e.target,()=>finish(true));
    $('avitoConfirmArea').scrollIntoView({block:'nearest',behavior:'smooth'});
  }
  async function decide(action,productId,reason,next=false){
    const d=view.detail;
    const nextId=view.items[view.items.findIndex(i=>i.id===d.listing.id)+1]?.id;
    await api('/api/avito/decisions',{listing_id:d.listing.id,listing_version:d.listing.version,link_version:d.link_version,action,product_id:productId,reason,product_snapshot:view.selected?snapshot(view.selected):null,dimension_unit:d.dimension_unit || '',acknowledged_warnings:action==='confirm'?(d.candidates.find(c=>c.product.id===productId)?.check.warnings || []):[]});
    await listings();await refreshPlans();
    if(action==='confirm')toast('Связь сохранена. На Авито ничего не изменено.');
    const target=next?(nextId || view.items.find(i=>i.id!==d.listing.id)?.id):null;
    await detail(target || d.listing.id);
  }
  function manual(a){
    showScreen('manual');
    const f=$('avitoManualForm');f.reset();f.hidden=false;
    for(const k of [...Object.keys(fields),'description'])f.elements[k].value=a?.[k] ?? '';
    for(const k of Object.keys(attributes))f.elements[k].value=a?.attributes?.[k] || '';
    f.elements.avito_id.readOnly=!!a;
    if(a?.observed_at){const d=new Date(a.observed_at);if(!isNaN(d)){d.setMinutes(d.getMinutes()-d.getTimezoneOffset());f.elements.observed_local.value=d.toISOString().slice(0,16);}}
    f.scrollIntoView({block:'start',behavior:'smooth'});
  }
  function renderPlans(rows){
    $('avitoPlan').hidden=view.mode!=='prices';
    const selected=(rows || []).filter(p=>p.factory===$('avitoFactory').value);
    $('avitoPlan').innerHTML='<div id="avitoPlanDetails"><div class="av2-empty"><h4>Сравнение ещё не открыто</h4><p>Нажмите «Сравнить цены» для свежей проверки или откройте сохранённое сравнение ниже.</p></div></div><details class="av2-plan-history"><summary>Ранее сохранённые сравнения</summary>'+(selected.length?selected.map(p=>'<button class="av2-history-row" data-avito-plan="'+esc(p.id)+'"><span>'+esc(date(p.created_at,true))+'</span><span>'+esc(plans[p.state] || p.state)+'</span></button>').join(''):'<p>Сравнений этой фабрики пока нет.</p>')+'</details>';
  }
  async function refreshPlans(){const data=await updateProgress();renderPlans(data.plans);}
  const priceGroup=i=>i.state==='unchanged'?'same':i.state==='blocked_transport'?'changed':'review';
  function renderPricePlan(p,filter='all'){
    const counts={all:p.items.length,changed:0,review:0,same:0};p.items.forEach(i=>counts[priceGroup(i)]++);
    const rows=p.items.filter(i=>filter==='all' || priceGroup(i)===filter);
    $('avitoPlanDetails').innerHTML='<div class="av2-screen-head"><div><h4>'+esc(p.factory)+' · '+esc(plans[p.state] || p.state)+'</h4><p class="av2-hint">'+esc(date(p.created_at,true))+'</p></div><button class="button secondary" data-avito-step="transfer">Как передать цены на Авито →</button></div>'+(p.state==='stale'?'<p class="notice error">Сравнение устарело. '+esc(p.invalidated_reason || 'Каталог изменился')+'. Подготовьте новое.</p>':'')+'<div class="av2-price-filters">'+Object.entries({all:'Все',changed:'С изменениями',review:'Нужна проверка',same:'Без изменений'}).map(([key,label])=>'<button data-avito-price-filter="'+key+'" aria-pressed="'+(filter===key)+'">'+label+' <b>'+counts[key]+'</b></button>').join('')+'</div>'+(p.is_demo?'<p class="avito-demo">В сравнении есть синтетические записи DEMO.</p>':'')+(p.items.length?'<div class="table-container"><table class="product-table av2-price-table"><thead><tr><th>Объявление / товар</th><th>Цена из выгрузки</th><th>Новая цена каталога</th><th>Разница</th><th>Что проверить</th></tr></thead><tbody>'+rows.map(i=>'<tr><td><strong>№ '+esc(i.avito_id)+'</strong><br>'+esc(i.product_snapshot.name)+'</td><td>'+cents(i.previous_cents)+'<br><small>'+esc(i.observed_at?date(i.observed_at,true):'Дата цены неизвестна')+'</small></td><td><strong>'+cents(i.target_cents)+'</strong></td><td>'+(i.previous_cents===null || i.target_cents===null?'—':cents(i.target_cents-i.previous_cents))+'</td><td><span class="av2-result '+priceGroup(i)+'">'+esc(states[i.state] || i.state)+'</span><details><summary>Подробнее</summary><p>'+i.reasons.map(esc).join('<br>')+'</p><small>'+esc(i.price_source)+'</small></details></td></tr>').join('')+'</tbody></table></div>'+(!rows.length?'<p class="av2-empty">В этой группе строк нет.</p>':''):'<div class="av2-empty"><h4>Нет связанных одиночных объявлений этой фабрики</h4><p>Сначала сохраните хотя бы одну связь с товаром каталога.</p><button class="button secondary" data-avito-step="matching">Выбрать объявления</button></div>')+(p.state==='prepared'?'<button class="text-button av2-cancel-plan" id="avitoCancelPlan">Отменить это сравнение</button>':'');
    if($('avitoCancelPlan'))$('avitoCancelPlan').onclick=e=>act(e.target,async()=>{await api('/api/avito/plans/'+p.id+'/cancel',{});await refreshPlans();await showPlan(p.id);});
  }
  async function showPlan(id){
    const p=await api('/api/avito/plans/'+id);
    if(p.factory!==$('avitoFactory').value)return;
    view.plan=p;renderPricePlan(p);
  }
  $('navAvito').onclick=open;
  $('avitoSetupButton').onclick=e=>act(e.target,async()=>{const r=await api('/api/avito/setup',{});toast(r.backup?'Резервная копия: '+r.backup:'Раздел уже подготовлен');await open();});
  $('avitoManual').onclick=()=>manual();$('avitoManualCancel').onclick=()=>showScreen('matching');
  $('avitoManualForm').onsubmit=e=>{e.preventDefault();const f=e.target;act(f.querySelector('button[type=submit]'),async()=>{
    const data=Object.fromEntries(new FormData(f));data.attributes=Object.fromEntries(Object.keys(attributes).map(k=>[k,data[k]]).filter(([,v])=>v));
    data.observed_at=data.observed_local?new Date(data.observed_local).toISOString():null;
    const r=await api('/api/avito/listings',data);f.hidden=true;await listings();await detail(r.id);await refreshPlans();
  });};
  $('avitoDemo').onclick=e=>act(e.target,async()=>{await api('/api/avito/demo',{});view.page=1;await listings();await refreshPlans();toast('Добавлены локальные тестовые записи DEMO.');});
  $('avitoPrepare').onclick=e=>act(e.target,async()=>{const p=await api('/api/avito/prepare',{factory:$('avitoFactory').value});showScreen('prices');await refreshPlans();await showPlan(p.id);});
  $('avitoJournalButton').onclick=e=>act(e.target,async()=>{const d=await api('/api/avito/journal');showScreen('journal');$('avitoHistory').innerHTML='<h3>Журнал действий на компьютере</h3><p>До 200 последних событий. Отправки на Авито нет.</p>'+d.items.map(i=>'<details><summary>'+esc(date(i.created_at,true))+' · '+esc(i.kind)+'</summary><pre>'+esc(i.details)+'</pre></details>').join('');});
  $('avitoFactory').onchange=async()=>{
    try{sessionStorage.setItem('avito.factory',$('avitoFactory').value);}catch{}
    view.page=1;view.detail=null;view.plan=null;$('avitoType').value='';$('avitoCollection').value='';$('avitoSearch').value='';$('avitoFilter').value='';
    const mode=view.mode==='prices'?'prices':'matching';
    renderPlans([]);showScreen(mode);
    try{await listings();await refreshPlans();if(view.mode===mode)showScreen(mode);}catch(e){fail(e);}
  };
  $('avitoFilter').onchange=()=>{view.page=1;listings().catch(fail);};
  let timer;$('avitoSearch').oninput=()=>{clearTimeout(timer);timer=setTimeout(()=>{view.page=1;listings().catch(fail);},250);};
  $('avitoSection').addEventListener('click',e=>{
    const b=e.target.closest('button');if(!b || b.disabled)return;
    if(b.dataset.avitoStep && !view.busy){showScreen(b.dataset.avitoStep);fail(null);}
    if(b.dataset.avitoPriceFilter && view.plan)renderPricePlan(view.plan,b.dataset.avitoPriceFilter);
    if(b.dataset.avitoOpen)detail(b.dataset.avitoOpen).catch(fail);
    if(b.dataset.avitoQueue){view.queue=b.dataset.avitoQueue;view.page=1;$('avitoType').value='';$('avitoCollection').value='';listings().catch(fail);}
    if(b.dataset.avitoChoose)choose(b.dataset.avitoChoose);
    if(b.dataset.avitoReject)act(b,()=>decide('reject',b.dataset.avitoReject,'Кандидат отклонён пользователем'));
    if(b.dataset.avitoDecision)act(b,()=>decide(b.dataset.avitoDecision,null,'Решение пользователя'));
    if(b.dataset.avitoPage){view.page=Number(b.dataset.avitoPage);listings().catch(fail);}
    if(b.dataset.avitoPlan)showPlan(b.dataset.avitoPlan).catch(fail);
  });
  window.AvitoUI={open,close};
})();
