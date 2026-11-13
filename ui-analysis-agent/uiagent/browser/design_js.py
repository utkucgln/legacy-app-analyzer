"""In-page design extractor: computed styles, component variants, legacy technology markers, layout and automated WCAG checks.

ES3 only (IE5-11 document modes in Edge IE mode): no JSON, forEach, indexOf on arrays, querySelector or trailing commas.
Uses getComputedStyle when present, otherwise IE currentStyle."""

DESIGN_JS = r"""
function (opts) {
  var LIMIT = 5000, maps = {}, a11y = {}, legacy = {}, legacySamples = {}, layout = {}, libs = {};
  var NAMED = {black: '#000000', white: '#ffffff', red: '#ff0000', green: '#008000', blue: '#0000ff', navy: '#000080', gray: '#808080',
               grey: '#808080', silver: '#c0c0c0', maroon: '#800000', yellow: '#ffff00', orange: '#ffa500', purple: '#800080',
               teal: '#008080', olive: '#808000', lime: '#00ff00', aqua: '#00ffff', fuchsia: '#ff00ff', windowtext: '#000000',
               window: '#ffffff', buttonface: '#f0f0f0', buttontext: '#000000', graytext: '#6d6d6d', highlight: '#0078d7',
               highlighttext: '#ffffff', threedface: '#f0f0f0', infobackground: '#ffffe1'};
  var KW = {'xx-small': 9, 'x-small': 10, 'small': 13, 'medium': 16, 'large': 18, 'x-large': 24, 'xx-large': 32};
  function clean(s) { return ('' + (s || '')).replace(/\s+/g, ' ').replace(/^\s+|\s+$/g, ''); }
  function attr(e, a) { var v = null; try { v = e.getAttribute(a); } catch (x) {} return v === null || v === undefined ? '' : '' + v; }
  function has(e, a) { try { return new RegExp('\\s' + a + '\\s*=', 'i').test((e.outerHTML || '').split('>')[0]); } catch (x) { return false; } }
  function cs(e) { try { if (window.getComputedStyle) { return window.getComputedStyle(e, null) || {}; } } catch (x) {} return e.currentStyle || {}; }
  function prop(st, css, ie) { var v = ''; try { if (st.getPropertyValue) { v = st.getPropertyValue(css); } } catch (x) {} if (!v) { v = st[ie]; } return v ? '' + v : ''; }
  function hex2(n) { n = Math.max(0, Math.min(255, n)); return (n < 16 ? '0' : '') + n.toString(16); }
  function color(s) {
    s = clean(s).toLowerCase();
    if (!s || s == 'transparent' || s == 'inherit' || s == 'initial' || s == 'currentcolor') { return null; }
    var m = /^rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)(?:[,\s\/]+([\d.]+))?/.exec(s);
    if (m) { if (m[4] !== undefined && m[4] !== '' && parseFloat(m[4]) < 0.5) { return null; } return '#' + hex2(+m[1]) + hex2(+m[2]) + hex2(+m[3]); }
    m = /^#([0-9a-f])([0-9a-f])([0-9a-f])$/.exec(s);
    if (m) { return '#' + m[1] + m[1] + m[2] + m[2] + m[3] + m[3]; }
    if (/^#[0-9a-f]{6}$/.test(s)) { return s; }
    return NAMED[s] || null;
  }
  function lum(h) {
    function c(v) { v = v / 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }
    return 0.2126 * c(parseInt(h.substr(1, 2), 16)) + 0.7152 * c(parseInt(h.substr(3, 2), 16)) + 0.0722 * c(parseInt(h.substr(5, 2), 16));
  }
  function contrast(a, b) { var x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); }
  function px(s) { s = clean(s).toLowerCase(); var m = /^(-?[\d.]+)(px|pt)?$/.exec(s); if (m) { var v = parseFloat(m[1]); return Math.round(m[2] == 'pt' ? v * 4 / 3 : v); } return KW[s] || 0; }
  function weight(s) { s = clean(s).toLowerCase(); if (s == 'bold' || s == 'bolder') { return 700; } if (!s || s == 'normal' || s == 'lighter') { return 400; } var n = parseInt(s, 10); return isNaN(n) ? 400 : n; }
  function family(s) { return clean(('' + s).split(',')[0]).replace(/["']/g, '').toLowerCase(); }
  function vis(e) { return !!(e.offsetWidth || e.offsetHeight); }
  function ownText(e) { var t = '', c = e.firstChild; while (c) { if (c.nodeType == 3) { t += c.nodeValue; } c = c.nextSibling; } return clean(t); }
  function txt(e) { var t = e.innerText; if (t === undefined || t === null) { t = e.textContent; } return clean(t).substring(0, 60); }
  function inc(name, key, extra) {
    if (key === null || key === undefined || key === '') { return; }
    var m = maps[name]; if (!m) { m = maps[name] = {}; }
    if (!m.hasOwnProperty(key)) { m[key] = {k: '' + key, n: 0, x: extra ? ('' + extra).substring(0, 60) : ''}; }
    m[key].n++;
  }
  function issue(rule, wcag, level, sample) {
    var r = a11y[rule]; if (!r) { r = a11y[rule] = {wcag: wcag, level: level, n: 0, samples: []}; }
    r.n++; if (sample && r.samples.length < 4) { r.samples.push(clean(sample).substring(0, 90)); }
  }
  function mark(name, sample) {
    legacy[name] = (legacy[name] || 0) + 1;
    if (sample) { var l = legacySamples[name]; if (!l) { l = legacySamples[name] = []; } if (l.length < 4) { l.push(clean(sample).substring(0, 90)); } }
  }
  function bgOf(e) {
    var p = e, d = 0;
    while (p && p.nodeType == 1 && d < 25) { var c = color(prop(cs(p), 'background-color', 'backgroundColor')) || color(attr(p, 'bgcolor')); if (c) { return c; } p = p.parentNode; d++; }
    return '#ffffff';
  }
  function sig(e, st, container) {
    var bw = px(prop(st, 'border-top-width', 'borderTopWidth')), bs = prop(st, 'border-top-style', 'borderTopStyle');
    return [color(prop(st, 'background-color', 'backgroundColor')) || '-', color(prop(st, 'color', 'color')) || '-',
            bw > 0 && bs != 'none' ? bw + 'px ' + bs + ' ' + (color(prop(st, 'border-top-color', 'borderTopColor')) || '') : 'none',
            'r' + px(prop(st, 'border-top-left-radius', 'borderTopLeftRadius')), px(prop(st, 'font-size', 'fontSize')) + 'px',
            weight(prop(st, 'font-weight', 'fontWeight')), container ? 'h-' : 'h' + (e.offsetHeight || 0),
            'p' + px(prop(st, 'padding-left', 'paddingLeft'))].join('|');
  }
  var CONTAINERS = {data_grid: 1, layout_table: 1, table_without_headers: 1, menu: 1, tabs: 1, message_banner: 1, fieldset: 1, image: 1};
  function comp(type, e, st, label) { inc('comp:' + type, sig(e, st, CONTAINERS[type] === 1), label); }
  function rect(e) { try { var r = e.getBoundingClientRect(); return {x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.right - r.left), h: Math.round(r.bottom - r.top)}; } catch (x) { return {x: 0, y: 0, w: 0, h: 0}; } }

  var labelled = {}, lbls = document.getElementsByTagName('label'), i, j, e;
  for (i = 0; i < lbls.length; i++) { var f = lbls[i].htmlFor || attr(lbls[i], 'for'); if (f) { labelled[f] = 1; } }
  function insideLabel(e) { var p = e.parentNode, d = 0; while (p && p.tagName && d < 6) { if (p.tagName.toLowerCase() == 'label') { return true; } p = p.parentNode; d++; } return false; }
  function hasName(e) { return !!(attr(e, 'aria-label') || attr(e, 'aria-labelledby') || attr(e, 'title') || (e.id && labelled[e.id]) || insideLabel(e)); }

  var vw = document.documentElement && document.documentElement.clientWidth ? document.documentElement.clientWidth : (document.body ? document.body.clientWidth : 0);
  var all = document.getElementsByTagName('*'), h1 = 0, headings = 0, smallTargets = 0, pageLinks = {};
  layout.nested_table_depth = 0; layout.left_nav = false; layout.top_bar = false; layout.max_right = 0;
  for (i = 0; i < all.length && i < LIMIT; i++) {
    e = all[i];
    var tn = (e.tagName || '').toLowerCase();
    if (!tn || tn == '!' || tn == 'script' || tn == 'style' || tn == 'head' || tn == 'meta' || tn == 'title' || tn == 'link' || tn == 'option' || tn == 'html') { continue; }
    var v = vis(e), st = cs(e), cls = ((e.className || '') + ' ' + (e.id || '')).toLowerCase(), type = (attr(e, 'type') || '').toLowerCase();
    if (v) {
      var r0 = rect(e); if (r0.x + r0.w > layout.max_right) { layout.max_right = r0.x + r0.w; }
      var own = ownText(e);
      if (own && own.length > 1) {
        var size = px(prop(st, 'font-size', 'fontSize')), wt = weight(prop(st, 'font-weight', 'fontWeight')), col = color(prop(st, 'color', 'color')) || '#000000', bg = bgOf(e);
        inc('typography', family(prop(st, 'font-family', 'fontFamily')) + '|' + size + '|' + wt, own);
        inc('text_color', col, own);
        var ratio = contrast(col, bg), large = size >= 24 || (size >= 19 && wt >= 700);
        if (ratio < (large ? 3 : 4.5)) { issue('contrast', '1.4.3', 'AA', own.substring(0, 40) + ' (' + col + ' on ' + bg + ', ' + ratio.toFixed(2) + ':1)'); }
        if (size > 0 && size < 11) { issue('small-text', '1.4.4', 'AA', own.substring(0, 40) + ' (' + size + 'px)'); }
      }
      var bgc = color(prop(st, 'background-color', 'backgroundColor'));
      if (bgc) { inc('background_color', bgc, tn); }
      var bw = px(prop(st, 'border-top-width', 'borderTopWidth')), bs = prop(st, 'border-top-style', 'borderTopStyle');
      if (bw > 0 && bs && bs != 'none') { inc('border_color', color(prop(st, 'border-top-color', 'borderTopColor')), tn); inc('border', bw + 'px ' + bs, tn); }
      var rad = px(prop(st, 'border-top-left-radius', 'borderTopLeftRadius')); if (rad > 0) { inc('radius', rad + 'px', tn); }
      if (/menu|nav|sidebar|tree/.test(cls) && r0.x < 60 && r0.w > 80 && r0.w < 320 && r0.h > 250) { layout.left_nav = true; }
      if (/head|banner|top|logo/.test(cls) && r0.y < 160 && r0.w > vw * 0.7) { layout.top_bar = true; }
    }
    if (tn == 'button' || (tn == 'input' && (type == 'submit' || type == 'button' || type == 'reset' || type == 'image')) || attr(e, 'role') == 'button' || (tn == 'a' && /btn|button/.test(cls))) {
      var bl = tn == 'input' ? clean(e.value || attr(e, 'alt') || attr(e, 'title')) : txt(e);
      if (v) { comp(type == 'image' ? 'image_button' : 'button', e, st, bl); }
      if (!bl && !attr(e, 'aria-label') && !attr(e, 'title')) { issue('button-name', '4.1.2', 'A', e.outerHTML ? e.outerHTML.substring(0, 80) : tn); }
    } else if (tn == 'input' && type != 'hidden') {
      var nm = (attr(e, 'name') + ' ' + (e.id || '')).toLowerCase();
      var kind = type == 'checkbox' ? 'checkbox' : type == 'radio' ? 'radio' : type == 'file' ? 'file_upload' :
                 (type == 'date' || /date|dob|_dt\b|dt_|fecha|tarikh/.test(nm)) ? 'date_input' : type == 'password' ? 'password_input' : 'text_input';
      if (v) { comp(kind, e, st, attr(e, 'name')); }
      if (!hasName(e) && kind != 'radio') { issue('form-label', '1.3.1 / 4.1.2', 'A', attr(e, 'name') || type); }
      if (attr(e, 'placeholder') && !hasName(e)) { issue('placeholder-as-label', '3.3.2', 'A', attr(e, 'name')); }
    } else if (tn == 'select' || tn == 'textarea') {
      if (v) { comp(tn == 'select' ? (e.multiple ? 'multi_select' : 'select') : 'textarea', e, st, attr(e, 'name')); }
      if (!hasName(e)) { issue('form-label', '1.3.1 / 4.1.2', 'A', attr(e, 'name') || tn); }
    } else if (tn == 'table') {
      var rows = e.rows ? e.rows.length : 0, ths = e.getElementsByTagName('th').length, ctl = e.getElementsByTagName('input').length + e.getElementsByTagName('select').length;
      var depth = 0, p = e.parentNode; while (p && p.tagName) { if (p.tagName.toLowerCase() == 'table') { depth++; } p = p.parentNode; }
      if (depth > layout.nested_table_depth) { layout.nested_table_depth = depth; }
      if (rows > 1 && v) {
        if (ths || e.getElementsByTagName('thead').length) { comp('data_grid', e, st, ths + ' cols'); }
        else if (ctl || e.getElementsByTagName('table').length || depth) { comp('layout_table', e, st, rows + ' rows'); mark('table-layout'); }
        else { comp('table_without_headers', e, st, rows + ' rows'); issue('table-headers', '1.3.1', 'A', txt(e).substring(0, 50)); }
      }
      if (attr(e, 'cellpadding')) { inc('cellpadding', attr(e, 'cellpadding') + 'px'); }
    } else if (tn == 'img') {
      var iw = e.width || e.offsetWidth, ih = e.height || e.offsetHeight;
      if (iw <= 2 || ih <= 2) { mark('spacer-image', attr(e, 'src')); }
      else if (v) { comp(iw <= 40 && ih <= 40 ? 'icon' : 'image', e, st, attr(e, 'alt') || attr(e, 'src').split('/').pop()); }
      if (!has(e, 'alt') && iw > 2) { issue('img-alt', '1.1.1', 'A', attr(e, 'src').split('/').pop()); }
      if (/cal|date/i.test(attr(e, 'src') + attr(e, 'alt'))) { comp('date_picker_trigger', e, st, attr(e, 'src').split('/').pop()); }
    } else if (tn == 'a') {
      var at = txt(e), href = attr(e, 'href');
      if (v) {
        if (/^(\d+|next|prev|previous|first|last|>>|<<|>|<|\u00bb|\u00ab)$/i.test(at)) { pageLinks[e.parentNode && e.parentNode.uniqueID ? e.parentNode.uniqueID : 'p'] = 1; comp('pagination_link', e, st, at); }
        else { comp(/menu|nav|tab/.test(cls) ? 'menu_link' : 'link', e, st, at); }
      }
      if (!at && !e.getElementsByTagName('img').length && !attr(e, 'title') && !attr(e, 'aria-label') && href) { issue('link-name', '2.4.4', 'A', href.substring(0, 60)); }
      if (/^(click here|here|more|link|go|details)$/i.test(at)) { issue('link-purpose', '2.4.4', 'A', at); }
      if (/^javascript:/i.test(href)) { mark('javascript-href', href.substring(0, 60)); }
    } else if (tn == 'fieldset') { if (v) { comp('fieldset', e, st, ''); } }
    else if (tn == 'iframe' || tn == 'frame') { mark(tn); if (!attr(e, 'title')) { issue('frame-title', '4.1.2', 'A', attr(e, 'src').substring(0, 60)); } }
    else if (tn == 'h1') { h1++; headings++; } else if (tn == 'h2' || tn == 'h3' || tn == 'h4') { headings++; }
    if (tn == 'ul' || tn == 'div' || tn == 'table' || tn == 'td') {
      if (/(^|[\s_-])tabs?([\s_-]|$)|tabstrip|tabbar/.test(cls) && v) { comp('tabs', e, st, txt(e).substring(0, 40)); }
      else if (/(^|[\s_-])(menu|nav|navbar|sidebar)([\s_-]|$)/.test(cls) && v) { comp('menu', e, st, txt(e).substring(0, 40)); }
      if (/error|alert|msg|message|warn|success|notice|info/.test(cls) && v && txt(e)) { comp('message_banner', e, st, txt(e)); }
    }
    if (tn == 'font') { mark('font-tag'); } else if (tn == 'center') { mark('center-tag'); } else if (tn == 'marquee' || tn == 'blink') { mark(tn + '-tag'); }
    else if (tn == 'frameset') { mark('frameset'); } else if (tn == 'applet') { mark('java-applet', attr(e, 'code')); } else if (tn == 'embed') { mark('embed', attr(e, 'src')); }
    else if (tn == 'object') { var cid = attr(e, 'classid'); mark(cid ? 'activex-object' : 'object-tag', cid || attr(e, 'data') || attr(e, 'type')); }
    if (attr(e, 'style')) { mark('inline-style'); }
    if (has(e, 'bgcolor') || has(e, 'align') || has(e, 'valign') || (tn != 'img' && has(e, 'width'))) { mark('presentational-attribute'); }
    var oc = attr(e, 'onclick') + attr(e, 'onchange') + attr(e, 'onload') + attr(e, 'onmouseover') + attr(e, 'onblur') + attr(e, 'onsubmit');
    if (oc) {
      mark('inline-event-handler');
      if (/showModalDialog|showModelessDialog/.test(oc)) { mark('modal-dialog', oc.substring(0, 80)); comp('modal_dialog_launcher', e, st, txt(e)); }
      if (/window\.open/.test(oc)) { mark('popup-window', oc.substring(0, 80)); comp('popup_launcher', e, st, txt(e)); }
      if (attr(e, 'onclick') && v && (tn == 'td' || tn == 'div' || tn == 'span' || tn == 'tr' || tn == 'img' || tn == 'li') && !attr(e, 'tabindex')) {
        issue('keyboard', '2.1.1', 'A', (txt(e) || tn).substring(0, 50)); comp('clickable_cell', e, st, txt(e));
      }
    }
    if (parseInt(attr(e, 'tabindex'), 10) > 0) { issue('tabindex-positive', '2.4.3', 'A', tn + ' ' + attr(e, 'tabindex')); }
    if (v && (tn == 'a' || tn == 'button' || (tn == 'input' && type != 'hidden')) && (e.offsetWidth < 24 || e.offsetHeight < 24) && type != 'checkbox' && type != 'radio' && tn != 'a') { smallTargets++; }
    if ((tn == 'font' && /red|#ff0000|#f00\b/i.test(attr(e, 'color'))) && v) { issue('color-only', '1.4.1', 'A', txt(e)); }
  }
  if (smallTargets) { a11y['target-size'] = {wcag: '2.5.8', level: 'AA', n: smallTargets, samples: []}; }
  var de = document.documentElement;
  if (!attr(de, 'lang') && !(document.body && attr(document.body, 'lang'))) { issue('html-lang', '3.1.1', 'A', ''); }
  if (!clean(document.title)) { issue('page-title', '2.4.2', 'A', ''); }
  if (!headings) { issue('no-headings', '1.3.1 / 2.4.6', 'A', ''); }
  var pl = 0, k; for (k in pageLinks) { if (pageLinks.hasOwnProperty(k)) { pl++; } } if (pl) { inc('comp:pagination', 'pager', pl + ' pagers'); }

  var scripts = document.getElementsByTagName('script'), src = '', code = '';
  for (i = 0; i < scripts.length; i++) {
    var s = scripts[i], lang = (attr(s, 'language') + ' ' + attr(s, 'type')).toLowerCase();
    if (/vbscript/.test(lang)) { mark('vbscript'); }
    if (attr(s, 'src')) { src += ' ' + attr(s, 'src').toLowerCase(); } else { code += ' ' + (s.text || s.innerHTML || '').substring(0, 20000); }
  }
  var probes = [['document-all', /document\.all\b/], ['show-modal-dialog', /showModalDialog/], ['attach-event', /attachEvent/],
                ['activex-script', /ActiveXObject/], ['eval', /\beval\s*\(/], ['document-write', /document\.write/], ['xmlhttprequest', /XMLHttpRequest/],
                ['ie-conditional-script', /@cc_on/]];
  for (i = 0; i < probes.length; i++) { if (probes[i][1].test(code)) { mark(probes[i][0]); } }
  var known = [['jquery', /jquery/], ['jquery-ui', /jquery[.-]ui/], ['prototype.js', /prototype(\.|-)/], ['dojo', /dojo/], ['extjs', /ext-(all|base)/],
               ['yui', /\byui/], ['dhtmlx', /dhtmlx/], ['telerik', /telerik|radcontrols/], ['infragistics', /infragistics|ig_/], ['struts-js', /struts/],
               ['dwr', /\/dwr\//], ['bootstrap', /bootstrap/], ['angularjs', /angular/], ['calendar-widget', /calendar|datepicker/]];
  var csshrefs = '', sheets = document.getElementsByTagName('link');
  for (i = 0; i < sheets.length; i++) { if (/stylesheet/i.test(attr(sheets[i], 'rel'))) { csshrefs += ' ' + attr(sheets[i], 'href').toLowerCase(); } }
  for (i = 0; i < known.length; i++) { if (known[i][1].test(src + ' ' + csshrefs + ' ' + code.substring(0, 3000).toLowerCase())) { libs[known[i][0]] = 1; } }
  var metas = document.getElementsByTagName('meta'), xua = '', viewport = '';
  for (i = 0; i < metas.length; i++) {
    var he = attr(metas[i], 'http-equiv').toLowerCase(), mn = attr(metas[i], 'name').toLowerCase();
    if (he == 'x-ua-compatible') { xua = attr(metas[i], 'content'); } if (mn == 'viewport') { viewport = attr(metas[i], 'content'); }
  }
  var out = {doc: {title: clean(document.title), url: '' + location.href, document_mode: document.documentMode || null,
                   compat_mode: document.compatMode || '', x_ua_compatible: xua, viewport_meta: viewport, lang: attr(de, 'lang'),
                   charset: document.charset || document.characterSet || '', stylesheets: document.styleSheets ? document.styleSheets.length : 0,
                   scripts: scripts.length, elements: all.length, h1: h1, headings: headings, viewport_width: vw,
                   page_width: document.body ? document.body.scrollWidth : 0, page_height: document.body ? document.body.scrollHeight : 0},
             maps: {}, a11y: a11y, legacy: legacy, legacy_samples: legacySamples, layout: layout, libs: []};
  for (k in libs) { if (libs.hasOwnProperty(k)) { out.libs.push(k); } }
  for (k in maps) {
    if (maps.hasOwnProperty(k)) {
      var arr = [], mm = maps[k], kk;
      for (kk in mm) { if (mm.hasOwnProperty(kk)) { arr.push(mm[kk]); } }
      arr.sort(function (a, b) { return b.n - a.n; });
      out.maps[k] = arr.slice(0, 40);
    }
  }
  return out;
}
"""
