// ---- Mailshare: кнопка «Отправить большой файл» в окне письма ----
// Версия 3.2: окно загрузки поверх письма; карточка из абзацев вставляется в конец письма
// (в ответах — перед цитатой). CKEditor 5 (SOGo 5.12+), CKEditor 4, простой текст.
(function () {
  var PAGE = '/mailshare/?embed=1';
  var TITLE = 'Отправить большие файлы ссылкой';
  var ICONS = ['attach_file', 'file_upload'];
  var overlay = null;   // { root, frame, ctx, busy }

  function log() {
    try { console.log.apply(console, ['mailshare:'].concat([].slice.call(arguments))); } catch (e) { /* ничего */ }
  }

  // ---------- Кнопка в окне письма ----------

  function composeOf(el) {
    var dialog = el.closest('md-dialog');
    if (dialog) return dialog;
    return /\/(new|reply|replyall|forward|edit)\b/.test(location.hash) ? document.body : null;
  }

  function addButtons() {
    var icons = document.querySelectorAll('md-icon');
    for (var i = 0; i < icons.length; i++) {
      var icon = icons[i];
      if (ICONS.indexOf(icon.textContent.trim()) < 0) continue;
      var compose = composeOf(icon);
      if (!compose || compose.querySelector('.mailshare-btn')) continue;
      var btn = icon.closest('button, .md-button, label');
      if (!btn || !btn.parentNode) continue;   // значок не в кнопке (например, зона перетаскивания)

      var b = document.createElement('button');
      b.type = 'button';
      b.className = btn.className + ' mailshare-btn';
      b.title = TITLE;
      b.setAttribute('aria-label', TITLE);
      var ic = icon.cloneNode(false);
      ic.textContent = 'cloud_upload';
      b.appendChild(ic);
      b.addEventListener('click', (function (ctx) {
        return function (e) {
          e.preventDefault();
          e.stopPropagation();
          openOverlay(ctx);
        };
      })(compose));
      btn.parentNode.insertBefore(b, btn.nextSibling);
    }
  }

  // ---------- Окно загрузки поверх письма ----------

  function openOverlay(ctx) {
    if (overlay) return;
    var root = document.createElement('div');
    root.className = 'mailshare-overlay';
    root.setAttribute('style', 'position:fixed;top:0;left:0;right:0;bottom:0;z-index:2147483000;' +
      'background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center');

    var panel = document.createElement('div');
    panel.setAttribute('style', 'width:640px;max-width:96vw;height:600px;max-height:92vh;background:#fff;' +
      'border-radius:8px;box-shadow:0 12px 40px rgba(0,0,0,.35);display:flex;flex-direction:column;overflow:hidden;' +
      'font-family:Roboto,Arial,sans-serif');

    var head = document.createElement('div');
    head.setAttribute('style', 'display:flex;align-items:center;justify-content:space-between;padding:12px 16px;' +
      'border-bottom:1px solid #e6e6e6;font-size:16px;font-weight:bold;color:#202124');
    head.appendChild(document.createTextNode('Отправка больших файлов'));
    var close = document.createElement('button');
    close.type = 'button';
    close.textContent = '×';
    close.title = 'Закрыть';
    close.setAttribute('style', 'border:none;background:none;font-size:24px;line-height:1;cursor:pointer;color:#5f6368;padding:0 4px');
    close.addEventListener('click', function () { closeOverlay(false); });
    head.appendChild(close);

    var frame = document.createElement('iframe');
    frame.src = PAGE;
    frame.setAttribute('allow', 'clipboard-write');
    frame.setAttribute('style', 'border:0;flex:1;width:100%');

    panel.appendChild(head);
    panel.appendChild(frame);
    root.appendChild(panel);
    document.body.appendChild(root);
    overlay = { root: root, frame: frame, ctx: ctx, busy: false };
    log('окно загрузки открыто');
  }

  function closeOverlay(force) {
    if (!overlay) return;
    var o = overlay;
    if (o.busy && !force) {
      if (!window.confirm('Загрузка ещё идёт. Прервать её?')) return;
      try { o.frame.contentWindow.postMessage({ type: 'mailshare:cancel' }, location.origin); } catch (e) { /* ничего */ }
      overlay = null;
      setTimeout(function () { o.root.remove(); }, 600);   // даём странице отменить загрузку
      return;
    }
    overlay = null;
    o.root.remove();
  }

  // ---------- Вставка карточки ----------

  function findEditor(ctx) {
    var C = window.CKEDITOR;
    if (!C || !C.instances) return null;
    for (var k in C.instances) {
      var ed = C.instances[k];
      try {
        var el = ed.container && ed.container.$;
        if (el && ctx.contains(el)) return ed;
      } catch (e) { /* ничего */ }
    }
    return null;
  }

  function findTextarea(ctx) {
    var best = null;
    var list = ctx.querySelectorAll('textarea');
    for (var i = 0; i < list.length; i++) {
      if (!best || list[i].offsetHeight > best.offsetHeight) best = list[i];
    }
    return best && best.offsetHeight > 40 ? best : null;
  }

  function findCk5(ctx) {
    var list = ctx.querySelectorAll('.ck-editor__editable');
    for (var i = 0; i < list.length; i++) if (list[i].ckeditorInstance) return list[i].ckeditorInstance;
    return null;
  }

  function modelText(el) {
    var out = '';
    try {
      var kids = Array.from(el.getChildren());
      for (var i = 0; i < kids.length; i++) out += kids[i].data || '';
    } catch (e) { /* ничего */ }
    return out.trim();
  }

  // Конец письма: перед цитатой (и её строкой «… писал(а):»), если это ответ; иначе — в самый конец
  function ck5EndPosition(ed) {
    var model = ed.model, root = model.document.getRoot();
    for (var i = 0; i < root.childCount; i++) {
      var ch = root.getChild(i);
      if (ch.is && ch.is('element', 'blockQuote')) {
        var prev = i > 0 ? root.getChild(i - 1) : null;
        if (prev && /:$/.test(modelText(prev))) return model.createPositionBefore(prev);
        return model.createPositionBefore(ch);
      }
    }
    return model.createPositionAt(root, 'end');
  }

  function insertCk5(ed, html) {
    var frag = ed.data.toModel(ed.data.processor.toView(html));
    ed.model.insertContent(frag, ck5EndPosition(ed));
    return 'ck5-конец письма';
  }

  function insertContent(ctx, html, text) {
    var ck5 = findCk5(ctx);
    if (ck5) return insertCk5(ck5, html);
    var ed = findEditor(ctx);
    if (ed) {
      ed.setData(ed.getData() + html);   // CKEditor 4: в конец письма
      return 'ck4-конец письма';
    }
    var ta = findTextarea(ctx);
    if (ta) {
      ta.value = ta.value.replace(/\s*$/, '') + '\n\n' + text + '\n';
      ta.dispatchEvent(new Event('input', { bubbles: true }));
      ta.dispatchEvent(new Event('change', { bubbles: true }));
      return 'текст-конец письма';
    }
    return null;
  }

  // ---------- Прикрепление PDF ----------

  function findFileInput(ctx) {
    var scopes = [ctx, document];
    for (var s = 0; s < scopes.length; s++) {
      var list = scopes[s].querySelectorAll('input[type=file]');
      for (var i = 0; i < list.length; i++) if (list[i].hasAttribute('nv-file-select')) return list[i];
      if (list.length) return list[0];
    }
    return null;
  }

  function attachFile(ctx, file) {
    var input = findFileInput(ctx);
    if (!input) throw new Error('не найдено поле вложений');
    var dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;   // тот же путь, что и при выборе файла скрепкой
    input.dispatchEvent(new Event('change', { bubbles: true }));
    return new Promise(function (resolve) {
      // Проверяем, что имя файла появилось в окне письма
      var tries = 0;
      (function check() {
        if (ctx.textContent.indexOf(file.name) >= 0) return resolve(true);
        if (++tries > 20) return resolve(false);
        setTimeout(check, 250);
      })();
    });
  }

  // ---------- Сообщения от окна загрузки ----------

  window.addEventListener('message', function (ev) {
    if (!overlay || ev.source !== overlay.frame.contentWindow || ev.origin !== location.origin) return;
    var d = ev.data || {};
    if (d.type === 'mailshare:state') {
      overlay.busy = !!d.busy;
    } else if (d.type === 'mailshare:close') {
      closeOverlay(true);
    } else if (d.type === 'mailshare:insert') {
      var o = overlay;
      var res = { type: 'mailshare:inserted', id: d.id, inserted: false, attached: false, errors: [] };
      var done = function () {
        log('вставка в письмо: ' + (res.mode || 'не вставлено') + ', PDF: ' + (res.attached ? 'да' : 'нет'), res);
        try { o.frame.contentWindow.postMessage(res, location.origin); } catch (e) { /* ничего */ }
        if (res.inserted && res.attached) setTimeout(function () { if (overlay === o) closeOverlay(true); }, 500);
      };
      try {
        var how = insertContent(o.ctx, d.html, d.text);
        res.inserted = !!how;
        res.mode = how;
        if (!how) res.errors.push('не найден редактор письма');
      } catch (e) { res.errors.push('текст: ' + e.message); }
      if (!d.file) { res.errors.push('PDF не получен'); return done(); }
      var p;
      try { p = attachFile(o.ctx, d.file); } catch (e) { res.errors.push('вложение: ' + e.message); return done(); }
      p.then(function (ok) {
        res.attached = ok;
        if (!ok) res.errors.push('вложение не появилось в письме');
        done();
      });
    }
  });

  // ---------- Диагностика: в консоли браузера выполнить mailshareDiag() ----------

  window.mailshareDiag = function () {
    var dialog = document.querySelector('md-dialog');
    var ctx = dialog || document.body;
    var inputs = ctx.querySelectorAll('input[type=file]');
    var info = {
      'окно письма (md-dialog)': !!dialog,
      'адрес': location.hash,
      'CKEDITOR загружен': !!window.CKEDITOR,
      'редакторов всего': window.CKEDITOR && window.CKEDITOR.instances ? Object.keys(window.CKEDITOR.instances).length : 0,
      'CKEditor 5 в окне письма': !!findCk5(ctx),
      'модули CKEditor 5': findCk5(ctx) ? [].map.call(Array.from(findCk5(ctx).plugins), function (x) { return x[0].pluginName; }).filter(Boolean).join(', ') : '',
      'CKEditor 4 в окне письма': !!findEditor(ctx),
      'поле текста (textarea)': !!findTextarea(ctx),
      'полей файлов в окне': inputs.length,
      'из них nv-file-select': [].filter.call(inputs, function (x) { return x.hasAttribute('nv-file-select'); }).length,
      'полей файлов на странице': document.querySelectorAll('input[type=file]').length,
      'кнопка Mailshare': !!ctx.querySelector('.mailshare-btn')
    };
    console.table(info);
    return info;
  };

  var timer = null;
  function schedule() {
    if (!timer) timer = setTimeout(function () { timer = null; addButtons(); }, 300);
  }
  function start() {
    new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
    schedule();
    log('кнопка подключена (v3.2)');
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
// ---- конец блока Mailshare ----
