(function () {
  'use strict';

  var CATS = [
    { key: 'money',    label: '变现路径' },
    { key: 'free',     label: '免费额度' },
    { key: 'industry', label: '行业动态' },
    { key: 'tip',      label: '避坑实操' }
  ];
  var CAT_MAP = {};
  CATS.forEach(function (c) { CAT_MAP[c.key] = c.label; });

  var FAV_KEY = 'ai_daily_favs_v1';

  var state = {
    index: null,
    dates: [],        // 有日报的日期，倒序
    dateSet: {},      // date -> issue meta
    data: null,
    cat: 'all',       // 'all' | cat key | 'favs'
    q: '',
    favs: {},
    calY: 0,
    calM: 0           // 月份 0-11
  };

  var $ = function (id) { return document.getElementById(id); };

  /* ---------------- favorites (localStorage, per-device) ---------------- */

  function loadFavs() {
    try {
      var raw = localStorage.getItem(FAV_KEY);
      state.favs = raw ? JSON.parse(raw) : {};
      if (typeof state.favs !== 'object' || Array.isArray(state.favs)) state.favs = {};
    } catch (e) { state.favs = {}; }
  }

  function saveFavs() {
    try { localStorage.setItem(FAV_KEY, JSON.stringify(state.favs)); }
    catch (e) { /* 隐私模式 / 存储满：静默失败 */ }
  }

  function favKey(date, id) { return date + '#' + id; }
  function isFaved(date, id) { return !!state.favs[favKey(date, id)]; }

  function toggleFav(item) {
    var k = favKey(item._date, item.id);
    if (state.favs[k]) { delete state.favs[k]; }
    else {
      state.favs[k] = {
        date: item._date, id: item.id,
        title: item.title, summary: item.summary, url: item.url,
        source: item.source, category: item.category, tags: item.tags || [],
        platform: item.platform || '', difficulty: item.difficulty || '',
        amount: item.amount || '', cycle: item.cycle || '',
        takeaway: item.takeaway || '', steps: item.steps || '',
        savedAt: Date.now()
      };
    }
    saveFavs();
    render();
  }

  function favList() {
    return Object.keys(state.favs).map(function (k) { return state.favs[k]; })
      .sort(function (a, b) { return (b.savedAt || 0) - (a.savedAt || 0); });
  }

  /* ---------------- utils ---------------- */

  function showState(msg, isError) {
    var el = $('state');
    if (!msg) { el.classList.add('hidden'); return; }
    el.classList.remove('hidden');
    el.classList.toggle('error', !!isError);
    $('stateMsg').textContent = msg;
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function fmtTs(iso) {
    if (!iso) return '—';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    var p = function (n) { return n < 10 ? '0' + n : '' + n; };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
           ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function fetchJSON(path) {
    return fetch(path + '?t=' + Date.now(), { cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status + ' @ ' + path);
        return r.json();
      });
  }

  /* ---------------- load ---------------- */

  function loadIndex() {
    showState('正在加载最新资讯…', false);
    return fetchJSON('data/index.json').then(function (idx) {
      if (!idx || !Array.isArray(idx.dates) || !idx.dates.length) {
        throw new Error('索引为空');
      }
      state.index = idx;
      state.dates = idx.dates.slice().sort().reverse();
      state.dateSet = {};
      (idx.issues || []).forEach(function (iss) {
        if (iss && iss.date) state.dateSet[iss.date] = iss;
      });
      state.dates.forEach(function (d) {
        if (!state.dateSet[d]) state.dateSet[d] = { date: d, title: '', itemCount: 0 };
      });
      state.pos = 0;

      var latest = state.dates[0].split('-');
      state.calY = parseInt(latest[0], 10);
      state.calM = parseInt(latest[1], 10) - 1;

      return loadDay(state.dates[0]);
    });
  }

  function loadDay(date) {
    showState('正在加载 ' + date + ' 的资讯…', false);
    return fetchJSON('data/daily/' + date + '.json').then(function (d) {
      if (!d || !Array.isArray(d.items)) throw new Error('数据格式异常：' + date);
      d.items.forEach(function (it) { it._date = d.date; });
      state.data = d;
      state.pos = state.dates.indexOf(d.date);
      if (state.pos < 0) state.pos = 0;
      if (state.cat !== 'favs') { state.cat = 'all'; }
      state.q = '';
      $('searchInput').value = '';
      render();
      showState(null);
    });
  }

  /* ---------------- calendar ---------------- */

  function renderCalendar() {
    var y = state.calY, m = state.calM;
    $('calLabel').textContent = y + ' 年 ' + (m + 1) + ' 月';

    var first = new Date(y, m, 1);
    var offset = (first.getDay() + 6) % 7;   // 周一为第一列
    var daysInMonth = new Date(y, m + 1, 0).getDate();

    var html = '';
    for (var i = 0; i < offset; i++) html += '<span class="cal-cell pad"></span>';

    var p = function (n) { return n < 10 ? '0' + n : '' + n; };
    var todayStr = (function () {
      var t = new Date();
      return t.getFullYear() + '-' + p(t.getMonth() + 1) + '-' + p(t.getDate());
    })();

    for (var day = 1; day <= daysInMonth; day++) {
      var ds = y + '-' + p(m + 1) + '-' + p(day);
      var has = state.dateSet[ds];
      var cls = 'cal-cell';
      if (has) cls += ' has';
      if (state.data && state.data.date === ds) cls += ' cur';
      if (ds === todayStr) cls += ' today';
      if (has) {
        var n = state.dateSet[ds].itemCount ? '<i>' + state.dateSet[ds].itemCount + '</i>' : '<i>·</i>';
        html += '<button class="' + cls + '" data-date="' + ds + '">' + day + n + '</button>';
      } else {
        html += '<span class="' + cls + '">' + day + '</span>';
      }
    }
    $('calGrid').innerHTML = html;
  }

  function calShift(delta) {
    var d = new Date(state.calY, state.calM + delta, 1);
    state.calY = d.getFullYear();
    state.calM = d.getMonth();
    renderCalendar();
  }

  /* ---------------- render ---------------- */

  function render() {
    var d = state.data;
    if (!d) return;

    $('cal').hidden = false;
    $('hero').hidden = false;
    $('toolbar').hidden = false;
    $('foot').hidden = false;

    $('heroTitle').textContent = d.title || ('AI 大模型每日资讯简报 - ' + d.date);
    $('heroMeta').textContent = '数据日期 ' + d.date + ' · 共 ' + (d.items.length) + ' 条';

    var sum = $('heroSummary');
    if (d.summary) { sum.textContent = d.summary; sum.style.display = ''; }
    else { sum.style.display = 'none'; }

    var count = {};
    CATS.forEach(function (c) { count[c.key] = 0; });
    d.items.forEach(function (it) { if (count[it.category] != null) count[it.category]++; });
    $('stats').innerHTML =
      '<div class="stat"><b>' + d.items.length + '</b><i>总计</i></div>' +
      CATS.map(function (c) {
        return '<div class="stat s-' + c.key + '"><b>' + count[c.key] + '</b><i>' + c.label + '</i></div>';
      }).join('');

    renderChips(count);
    renderCalendar();
    renderArchive();
    renderList();
  }

  function renderChips(count) {
    var favN = Object.keys(state.favs).length;
    var chipHtml = '<button class="chip' + (state.cat === 'all' ? ' on' : '') + '" data-cat="all">全部<span class="n">' + state.data.items.length + '</span></button>';
    chipHtml += CATS.map(function (c) {
      return '<button class="chip' + (state.cat === c.key ? ' on' : '') + '" data-cat="' + c.key + '">' +
             c.label + '<span class="n">' + count[c.key] + '</span></button>';
    }).join('');
    chipHtml += '<button class="chip chip-fav' + (state.cat === 'favs' ? ' on' : '') + '" data-cat="favs">★ 我的收藏<span class="n">' + favN + '</span></button>';
    $('chips').innerHTML = chipHtml;
  }

  /* ---------------- archive ---------------- */

  function renderArchive() {
    var issues = state.dates.map(function (d) { return state.dateSet[d] || { date: d }; });
    $('archive').hidden = issues.length < 2;
    if ($('archive').hidden) return;

    $('archiveCount').textContent = issues.length + ' 期';
    var cur = state.data ? state.data.date : '';

    $('archiveBody').innerHTML = issues.map(function (iss) {
      var isCur = iss.date === cur;
      var isLatest = iss.date === state.dates[0];
      var n = iss.itemCount ? (iss.itemCount + ' 条') : '';
      return '<button class="a-row' + (isCur ? ' cur' : '') + '" data-date="' + esc(iss.date) + '">' +
               '<span class="a-date">' + esc(iss.date) + (isLatest ? ' <em class="a-new">最新</em>' : '') + '</span>' +
               '<span class="a-title">' + esc(iss.title || '') + '</span>' +
               '<span class="a-n">' + esc(n) + '</span>' +
             '</button>';
    }).join('');
  }

  /* ---------------- list ---------------- */

  function guideBlock(it) {
    // money 类的新手指南块：难度 / 已赚金额 / 周期 / 小结 / 入手步骤
    var rows = [];
    if (it.difficulty) rows.push(['上手难度', it.difficulty]);
    if (it.amount) rows.push(['已赚金额', it.amount]);
    if (it.cycle) rows.push(['变现周期', it.cycle]);
    if (it.platform) rows.push(['主要平台', it.platform]);
    if (!rows.length && !it.takeaway && !it.steps) return '';

    var grid = rows.length
      ? '<div class="g-grid">' + rows.map(function (r) {
          return '<div class="g-item"><span>' + esc(r[0]) + '</span><b>' + esc(r[1]) + '</b></div>';
        }).join('') + '</div>'
      : '';

    var take = it.takeaway ? '<p class="g-line"><b>小结：</b>' + esc(it.takeaway) + '</p>' : '';
    var steps = it.steps ? '<p class="g-line"><b>新手入手：</b>' + esc(it.steps) + '</p>' : '';
    if (!grid && !take && !steps) return '';

    return '<div class="guide"><div class="g-head">🧭 新手指南</div>' + grid + take + steps + '</div>';
  }

  function itemCard(it, opts) {
    opts = opts || {};
    var cat = it.category || 'industry';
    var label = CAT_MAP[cat] || cat;
    var tags = (it.tags || []).map(function (t) { return '<span class="tag">#' + esc(t) + '</span>'; }).join('');
    var action = it.action
      ? '<div class="card-action"><b>可执行：</b>' + esc(it.action) + '</div>'
      : '';
    var faved = isFaved(it._date, it.id);
    var guide = (cat === 'money' || it.takeaway || it.steps) ? guideBlock(it) : '';

    var footLeft = opts.showDate
      ? '<span class="src">' + esc(it._date || it.date || '') + (it.source ? ' · ' + esc(it.source) : '') + '</span>'
      : '<span class="src">来源：' + esc(it.source || '未标注') + '</span>';

    return '' +
      '<article class="card cat-' + cat + '">' +
        '<div class="card-head">' +
          '<span class="badge cat-' + cat + '">' + esc(label) + '</span>' +
          '<button class="fav-btn' + (faved ? ' on' : '') + '" data-fav="' + esc(it._date) + '#' + esc(it.id) + '" aria-label="收藏">' +
            (faved ? '★' : '☆') + '</button>' +
        '</div>' +
        '<h2>' + esc(it.title) + '</h2>' +
        '<p>' + esc(it.summary) + '</p>' +
        action +
        guide +
        (tags ? '<div class="tags">' + tags + '</div>' : '') +
        '<div class="card-foot">' + footLeft +
          (it.url ? '<a class="link" href="' + esc(it.url) + '" target="_blank" rel="noopener noreferrer">查看原文 ↗</a>' : '') +
        '</div>' +
      '</article>';
  }

  function renderList() {
    if (state.cat === 'favs') {
      var favs = favList();
      if (!favs.length) {
        $('list').innerHTML = '<div class="empty">还没有收藏。<br><br>在任意资讯卡片右上角点 <b>☆</b> 即可收藏，<br>收藏保存在本机浏览器中，随时回来看。</div>';
        return;
      }
      $('list').innerHTML = favs.map(function (it) {
        var copy = Object.assign({}, it, { _date: it.date });
        return itemCard(copy, { showDate: true });
      }).join('');
      return;
    }

    var d = state.data;
    var q = state.q.trim().toLowerCase();

    var items = d.items.filter(function (it) {
      if (state.cat !== 'all' && it.category !== state.cat) return false;
      if (!q) return true;
      var hay = [it.title, it.summary, it.source, it.action, it.platform,
                 it.difficulty, it.amount, it.cycle, it.takeaway, it.steps,
                 (it.tags || []).join(' ')].join(' ').toLowerCase();
      return hay.indexOf(q) !== -1;
    });

    if (!items.length) {
      $('list').innerHTML = '<div class="empty">没有匹配的资讯。<br>试试换个关键词，或切回「全部」分类。</div>';
      return;
    }

    $('list').innerHTML = items.map(function (it) { return itemCard(it); }).join('');
  }

  /* ---------------- events ---------------- */

  $('btnRefresh').addEventListener('click', function () {
    var b = $('btnRefresh');
    b.classList.add('spinning');
    loadIndex()
      .catch(function (e) { showState('刷新失败：' + e.message + '\n请检查网络后重试。', true); })
      .then(function () { b.classList.remove('spinning'); });
  });

  $('calPrev').addEventListener('click', function () { calShift(-1); });
  $('calNext').addEventListener('click', function () { calShift(1); });

  $('calGrid').addEventListener('click', function (e) {
    var cell = e.target.closest('button.cal-cell');
    if (!cell) return;
    var date = cell.getAttribute('data-date');
    if (!date || state.dates.indexOf(date) === -1) return;
    loadDay(date).then(function () { window.scrollTo({ top: 0, behavior: 'smooth' }); })
      .catch(function (er) { showState('加载失败：' + er.message, true); });
  });

  $('btnArchive').addEventListener('click', function () {
    var open = $('archiveBody').hidden;
    $('archiveBody').hidden = !open;
    $('archiveArrow').textContent = open ? '▴' : '▾';
    $('btnArchive').setAttribute('aria-expanded', open ? 'true' : 'false');
  });

  $('archiveBody').addEventListener('click', function (e) {
    var row = e.target.closest('.a-row');
    if (!row) return;
    var date = row.getAttribute('data-date');
    var idx = state.dates.indexOf(date);
    if (idx === -1) return;
    loadDay(date)
      .then(function () {
        $('archiveBody').hidden = true;
        $('archiveArrow').textContent = '▾';
        window.scrollTo({ top: 0, behavior: 'smooth' });
      })
      .catch(function (er) { showState('加载失败：' + er.message, true); });
  });

  $('chips').addEventListener('click', function (e) {
    var btn = e.target.closest('.chip');
    if (!btn) return;
    state.cat = btn.getAttribute('data-cat');
    Array.prototype.forEach.call($('chips').children, function (c) {
      c.classList.toggle('on', c.getAttribute('data-cat') === state.cat);
    });
    renderList();
  });

  $('list').addEventListener('click', function (e) {
    var btn = e.target.closest('.fav-btn');
    if (!btn) return;
    var parts = btn.getAttribute('data-fav').split('#');
    var date = parts[0], id = parts[1];

    var item = null;
    if (state.data) {
      for (var i = 0; i < state.data.items.length; i++) {
        if (String(state.data.items[i].id) === String(id)) { item = state.data.items[i]; break; }
      }
    }
    if (!item && state.favs[date + '#' + id]) { item = state.favs[date + '#' + id]; }
    if (!item) return;
    item._date = date;
    toggleFav(item);
  });

  var timer = null;
  $('searchInput').addEventListener('input', function (e) {
    var v = e.target.value;
    clearTimeout(timer);
    timer = setTimeout(function () { state.q = v; renderList(); }, 120);
  });

  /* ---------------- boot ---------------- */

  loadFavs();

  if (location.protocol === 'file:') {
    showState('请通过 http(s) 访问本页（浏览器直接双击打开 .html 无法读取数据文件）。', true);
  } else {
    loadIndex().catch(function (e) {
      showState('数据加载失败：' + e.message + '\n本期资讯尚未生成，请稍后再试。', true);
    });
  }
})();
