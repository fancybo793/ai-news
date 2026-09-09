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
    dates: [],
    pos: 0,
    data: null,
    cat: 'all',          // 'all' | cat key | 'favs'
    q: '',
    favs: {}             // key: date#id -> item snapshot
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
    catch (e) { /* 存储满或隐私模式：静默失败，不打断浏览 */ }
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
      state.dates = idx.dates.slice().sort().reverse(); // 最新在前
      state.pos = 0;
      return loadDay(state.dates[0]);
    });
  }

  function loadDay(date) {
    showState('正在加载 ' + date + ' 的资讯…', false);
    return fetchJSON('data/daily/' + date + '.json').then(function (d) {
      if (!d || !Array.isArray(d.items)) throw new Error('数据格式异常：' + date);
      d.items.forEach(function (it) { it._date = d.date; });
      state.data = d;
      if (state.cat !== 'favs') { state.cat = 'all'; }
      state.q = '';
      $('searchInput').value = '';
      render();
      showState(null);
    });
  }

  /* ---------------- render ---------------- */

  function render() {
    var d = state.data;
    if (!d) return;

    $('dateNav').hidden = state.dates.length < 1;
    $('hero').hidden = false;
    $('toolbar').hidden = false;
    $('foot').hidden = false;

    $('navDate').textContent = d.date;
    $('navPos').textContent = '第 ' + (state.pos + 1) + ' / ' + state.dates.length + ' 期';
    $('btnPrev').disabled = state.pos >= state.dates.length - 1;
    $('btnNext').disabled = state.pos <= 0;

    $('heroTitle').textContent = d.title || ('AI 大模型每日资讯简报 - ' + d.date);
    $('heroMeta').textContent = '数据日期 ' + d.date + ' · 共 ' + (d.items.length) + ' 条';

    var sum = $('heroSummary');
    if (d.summary) { sum.textContent = d.summary; sum.style.display = ''; }
    else { sum.style.display = 'none'; }

    // stats
    var count = {};
    CATS.forEach(function (c) { count[c.key] = 0; });
    d.items.forEach(function (it) { if (count[it.category] != null) count[it.category]++; });
    $('stats').innerHTML =
      '<div class="stat"><b>' + d.items.length + '</b><i>总计</i></div>' +
      CATS.map(function (c) {
        return '<div class="stat s-' + c.key + '"><b>' + count[c.key] + '</b><i>' + c.label + '</i></div>';
      }).join('');

    renderChips(count);
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
    var issues = (state.index && Array.isArray(state.index.issues) && state.index.issues.length)
      ? state.index.issues
      : state.dates.map(function (d) { return { date: d }; });

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

  function itemCard(it, opts) {
    opts = opts || {};
    var cat = it.category || 'industry';
    var label = CAT_MAP[cat] || cat;
    var tags = (it.tags || []).map(function (t) { return '<span class="tag">#' + esc(t) + '</span>'; }).join('');
    var action = it.action
      ? '<div class="card-action"><b>可执行：</b>' + esc(it.action) + '</div>'
      : '';
    var faved = isFaved(it._date, it.id);

    // 新手拆解元信息：来源平台 / 难易度 / 变现金额 / 变现周期
    var metas = [];
    if (it.platform) metas.push('📡 ' + it.platform);
    if (it.difficulty) metas.push('🪜 难度 ' + it.difficulty);
    if (it.amount) metas.push('💰 ' + it.amount);
    if (it.cycle) metas.push('⏱ ' + it.cycle);
    var metaRow = metas.length
      ? '<div class="meta-row">' + metas.map(function (m) { return '<span class="meta">' + esc(m) + '</span>'; }).join('') + '</div>'
      : '';

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
        metaRow +
        action +
        (tags ? '<div class="tags">' + tags + '</div>' : '') +
        '<div class="card-foot">' + footLeft +
          (it.url ? '<a class="link" href="' + esc(it.url) + '" target="_blank" rel="noopener noreferrer">查看原文 ↗</a>' : '') +
        '</div>' +
      '</article>';
  }

  function renderList() {
    // —— 收藏视图：跨日期显示所有已收藏条目 ——
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

    // —— 普通视图 ——
    var d = state.data;
    var q = state.q.trim().toLowerCase();

    var items = d.items.filter(function (it) {
      if (state.cat !== 'all' && it.category !== state.cat) return false;
      if (!q) return true;
      var hay = [it.title, it.summary, it.source, it.action, it.platform,
                 it.difficulty, it.amount, it.cycle, (it.tags || []).join(' ')]
        .join(' ').toLowerCase();
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

  $('btnPrev').addEventListener('click', function () {
    if (state.pos >= state.dates.length - 1) return;
    state.pos++;
    loadDay(state.dates[state.pos]).catch(function (e) { showState('加载失败：' + e.message, true); });
  });

  $('btnNext').addEventListener('click', function () {
    if (state.pos <= 0) return;
    state.pos--;
    loadDay(state.dates[state.pos]).catch(function (e) { showState('加载失败：' + e.message, true); });
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
    state.pos = idx;
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

  // file:// 直开时 fetch 会被 CORS 拦截，给出明确提示而不是静默失败
  if (location.protocol === 'file:') {
    showState('请通过 http(s) 访问本页（浏览器直接双击打开 .html 无法读取数据文件）。', true);
  } else {
    loadIndex().catch(function (e) {
      showState('数据加载失败：' + e.message + '\n本期资讯尚未生成，请稍后再试。', true);
    });
  }
})();
