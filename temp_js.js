
// =========== Constants ===========
const INITIAL_EACH = 100;
const INITIAL_TOTAL = 400;
const WARN_THRESHOLD = 30;
const CATEGORIES = ['gold', 'nasdaq', 'dividend', 'cash'];
const LABELS = { gold: '黄金', nasdaq: '纳指', dividend: '红利', cash: '现金' };
const CODES = { gold: '518850', nasdaq: '159696', dividend: '563020', cash: '货币基金' };
const COLORS = {
  gold: '#f0b90b',
  nasdaq: '#3861fb',
  dividend: '#e84118',
  cash: '#44bd32'
};
const STORAGE_KEY = 'permanent_portfolio_history';

// =========== State ===========
let history = [];
let pieChart = null;

// =========== Init ===========
document.addEventListener('DOMContentLoaded', () => {
  loadFromStorage();
  const today = new Date().toISOString().slice(0, 10);
  document.getElementById('inputDate').value = today;
  renderAll();
});

// =========== Data Persistence ===========
function loadFromStorage() {
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved) {
    try { history = JSON.parse(saved); } catch(e) { history = []; }
  }
  if (history.length) history.sort((a, b) => a.date.localeCompare(b.date));
}

function saveToStorage() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(history));
}

function exportData() {
  if (history.length === 0) {
    alert('没有数据可导出');
    return;
  }
  const blob = new Blob([JSON.stringify(history, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  const now = new Date().toISOString().slice(0, 10);
  a.download = `永久投资组合数据_${now}.json`;
  a.click();
  URL.revokeObjectURL(url);

  const note = document.getElementById('syncNote');
  note.textContent = '已导出，换设备后使用"导入数据"按钮恢复';
  setTimeout(() => {
    note.textContent = '数据保存在本机浏览器中，换设备请导出后导入';
  }, 3000);
}

function importData(event) {
  const file = event.target.files[0];
  if (!file) return;

  const reader = new FileReader();
  reader.onload = function(e) {
    try {
      const imported = JSON.parse(e.target.result);
      if (!Array.isArray(imported)) {
        alert('文件格式不正确，需要 JSON 数组');
        return;
      }
      for (const entry of imported) {
        if (!entry.date || typeof entry.gold !== 'number' ||
            typeof entry.nasdaq !== 'number' || typeof entry.dividend !== 'number' ||
            typeof entry.cash !== 'number') {
          alert('文件数据格式不正确');
          return;
        }
      }

      const existingDates = new Set(history.map(h => h.date));
      if (imported.some(e => existingDates.has(e.date))) {
        const choice = confirm(
          '导入数据中有日期与现有数据重叠。\n\n' +
          '点击"确定"：用导入数据覆盖重叠日期，保留不重叠的旧数据\n' +
          '点击"取消"：放弃导入'
        );
        if (!choice) { event.target.value = ''; return; }
        const importDates = new Set(imported.map(e => e.date));
        history = history.filter(h => !importDates.has(h.date));
        history = [...history, ...imported];
      } else {
        history = [...history, ...imported];
      }

      history.sort((a, b) => a.date.localeCompare(b.date));
      saveToStorage();
      renderAll();

      const note = document.getElementById('syncNote');
      note.textContent = `已导入 ${imported.length} 条记录`;
      setTimeout(() => {
        note.textContent = '数据保存在本机浏览器中，换设备请导出后导入';
      }, 3000);
    } catch(err) {
      alert('文件解析失败：' + err.message);
    }
  };
  reader.readAsText(event.target.files[0]);
  event.target.value = '';
}

// =========== Calculation Helpers ===========
function getLatestValues() {
  if (history.length === 0) {
    return { gold: INITIAL_EACH, nasdaq: INITIAL_EACH, dividend: INITIAL_EACH, cash: INITIAL_EACH };
  }
  const latest = history[history.length - 1];
  return {
    gold: latest.gold,
    nasdaq: latest.nasdaq,
    dividend: latest.dividend,
    cash: latest.cash
  };
}

function getPreviousValues() {
  if (history.length === 0) {
    return { gold: INITIAL_EACH, nasdaq: INITIAL_EACH, dividend: INITIAL_EACH, cash: INITIAL_EACH };
  }
  if (history.length === 1) {
    return { gold: INITIAL_EACH, nasdaq: INITIAL_EACH, dividend: INITIAL_EACH, cash: INITIAL_EACH };
  }
  const prev = history[history.length - 2];
  return {
    gold: prev.gold,
    nasdaq: prev.nasdaq,
    dividend: prev.dividend,
    cash: prev.cash
  };
}

function calcReturn(current, initial) {
  if (initial === 0) return 0;
  return ((current - initial) / initial * 100);
}

function formatMoney(val) {
  return '¥' + Number(val).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatPct(val, withSign) {
  const v = Number(val).toFixed(2);
  if (withSign && v > 0) return '+' + v + '%';
  return v + '%';
}

function getReturnClass(val) {
  if (val > 0) return 'positive';
  if (val < 0) return 'negative';
  return '';
}

// =========== Actions ===========
function addEntry() {
  const date = document.getElementById('inputDate').value;
  const gold = parseFloat(document.getElementById('inputGold').value);
  const nasdaq = parseFloat(document.getElementById('inputNasdaq').value);
  const dividend = parseFloat(document.getElementById('inputDividend').value);
  const cash = parseFloat(document.getElementById('inputCash').value);

  if (!date) { alert('请选择日期'); return; }
  if (isNaN(gold) || isNaN(nasdaq) || isNaN(dividend) || isNaN(cash)) {
    alert('请填写所有金额'); return;
  }
  if (gold < 0 || nasdaq < 0 || dividend < 0 || cash < 0) {
    alert('金额不能为负数'); return;
  }

  const existingIdx = history.findIndex(h => h.date === date);
  const entry = { date, gold, nasdaq, dividend, cash };

  if (existingIdx >= 0) {
    if (!confirm(`日期 ${date} 已有记录，是否覆盖？`)) return;
    history[existingIdx] = entry;
  } else {
    history.push(entry);
  }

  history.sort((a, b) => a.date.localeCompare(b.date));
  saveToStorage();
  renderAll();
  clearInputs();
}

function deleteEntry(index) {
  if (!confirm('确认删除该记录？')) return;
  history.splice(index, 1);
  saveToStorage();
  renderAll();
}

function resetAll() {
  if (!confirm('确认删除所有历史记录？此操作不可撤销！')) return;
  history = [];
  saveToStorage();
  renderAll();
}

function clearInputs() {
  document.getElementById('inputGold').value = '';
  document.getElementById('inputNasdaq').value = '';
  document.getElementById('inputDividend').value = '';
  document.getElementById('inputCash').value = '';
}

// =========== Render ===========
function renderAll() {
  const vals = getLatestValues();
  const prevVals = getPreviousValues();
  const total = vals.gold + vals.nasdaq + vals.dividend + vals.cash;
  const prevTotal = prevVals.gold + prevVals.nasdaq + prevVals.dividend + prevVals.cash;

  CATEGORIES.forEach(cat => {
    const amt = vals[cat];
    const ret = calcReturn(amt, INITIAL_EACH);
    const ratio = total > 0 ? (amt / total * 100) : 0;
    const deltaPct = calcReturn(amt, prevVals[cat]);

    document.getElementById('amt' + capitalize(cat)).textContent = formatMoney(amt);

    const retEl = document.getElementById('ret' + capitalize(cat));
    retEl.textContent = formatPct(ret, true);
    retEl.className = 'return-val ' + getReturnClass(ret);

    const deltaEl = document.getElementById('delta' + capitalize(cat));
    deltaEl.textContent = formatPct(deltaPct, true);
    deltaEl.className = 'return-val ' + getReturnClass(deltaPct);

    const ratioEl = document.getElementById('ratio' + capitalize(cat));
    ratioEl.textContent = '占比 ' + ratio.toFixed(1) + '%';
    ratioEl.className = 'ratio-badge ' + (ratio > WARN_THRESHOLD ? 'warn' : 'normal');
  });

  const totalRet = calcReturn(total, INITIAL_TOTAL);
  const totalDeltaAmt = total - prevTotal;
  const totalDeltaPct = calcReturn(total, prevTotal);

  document.getElementById('totalAmt').textContent = formatMoney(total);

  const totalRetEl = document.getElementById('totalRet');
  totalRetEl.textContent = formatPct(totalRet, true);
  totalRetEl.className = getReturnClass(totalRet);

  const totalDeltaAmtEl = document.getElementById('totalDeltaAmt');
  totalDeltaAmtEl.textContent = (totalDeltaAmt >= 0 ? '+' : '') + formatMoney(totalDeltaAmt);
  totalDeltaAmtEl.className = getReturnClass(totalDeltaAmt);

  const totalDeltaPctEl = document.getElementById('totalDeltaPct');
  totalDeltaPctEl.textContent = '收益率 ' + formatPct(totalDeltaPct, true);
  totalDeltaPctEl.className = 'sub-info ' + getReturnClass(totalDeltaPct);

  const overThreshold = CATEGORIES.filter(cat => {
    return total > 0 && (vals[cat] / total * 100) > WARN_THRESHOLD;
  });
  const warnBox = document.getElementById('rebalanceWarning');
  const overItems = document.getElementById('overItems');
  if (overThreshold.length > 0) {
    warnBox.classList.add('show');
    overItems.textContent = '超标类别：' + overThreshold.map(c => LABELS[c]).join('、');
  } else {
    warnBox.classList.remove('show');
  }

  renderChart(vals);
  renderHistory();
}

function renderChart(vals) {
  const ctx = document.getElementById('pieChart').getContext('2d');
  if (pieChart) pieChart.destroy();

  const data = CATEGORIES.map(cat => vals[cat]);
  const total = data.reduce((a, b) => a + b, 0);
  const pcts = data.map(d => total > 0 ? (d / total * 100).toFixed(1) : '0.0');

  pieChart = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: CATEGORIES.map((c, i) => LABELS[c] + ' ' + pcts[i] + '%'),
      datasets: [{
        data: data,
        backgroundColor: CATEGORIES.map(c => COLORS[c]),
        borderColor: '#fff',
        borderWidth: 2,
        hoverBorderWidth: 3,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: true,
      plugins: {
        legend: {
          position: 'bottom',
          labels: {
            padding: 16,
            usePointStyle: true,
            pointStyleWidth: 10,
            font: { size: 13, family: '-apple-system, "Microsoft YaHei", sans-serif' }
          }
        },
        tooltip: {
          callbacks: {
            label: function(ctx) {
              return ctx.label + ': ' + formatMoney(ctx.raw);
            }
          }
        }
      },
      cutout: '60%',
    }
  });
}

function renderHistory() {
  const tbody = document.getElementById('historyBody');
  const noData = document.getElementById('noData');

  if (history.length === 0) {
    tbody.innerHTML = '';
    noData.style.display = 'block';
    return;
  }

  noData.style.display = 'none';
  tbody.innerHTML = history.map((h, i) => {
    const total = h.gold + h.nasdaq + h.dividend + h.cash;
    const totalRet = calcReturn(total, INITIAL_TOTAL);

    const prevH = i > 0 ? history[i - 1] : null;
    const prevTotal = prevH ? (prevH.gold + prevH.nasdaq + prevH.dividend + prevH.cash) : INITIAL_TOTAL;

    const deltaAmt = total - prevTotal;
    const deltaPct = calcReturn(total, prevTotal);

    const catDeltas = {};
    CATEGORIES.forEach(cat => {
      const prevAmt = prevH ? prevH[cat] : INITIAL_EACH;
      catDeltas[cat] = calcReturn(h[cat], prevAmt);
    });

    const showDelta = i > 0 || history.length > 1;

    return `
      <tr>
        <td>${h.date}${i === history.length - 1 ? ' <span style="font-size:10px;color:#3861fb;font-weight:600;">最新</span>' : ''}</td>
        <td>
          <span class="cell-main">${formatMoney(h.gold)}</span>
          ${showDelta ? `<span class="cell-delta ${getReturnClass(catDeltas.gold)}">${formatPct(catDeltas.gold, true)}</span>` : ''}
        </td>
        <td>
          <span class="cell-main">${formatMoney(h.nasdaq)}</span>
          ${showDelta ? `<span class="cell-delta ${getReturnClass(catDeltas.nasdaq)}">${formatPct(catDeltas.nasdaq, true)}</span>` : ''}
        </td>
        <td>
          <span class="cell-main">${formatMoney(h.dividend)}</span>
          ${showDelta ? `<span class="cell-delta ${getReturnClass(catDeltas.dividend)}">${formatPct(catDeltas.dividend, true)}</span>` : ''}
        </td>
        <td>
          <span class="cell-main">${formatMoney(h.cash)}</span>
          ${showDelta ? `<span class="cell-delta ${getReturnClass(catDeltas.cash)}">${formatPct(catDeltas.cash, true)}</span>` : ''}
        </td>
        <td>${formatMoney(total)}</td>
        <td class="${getReturnClass(deltaAmt)}">
          <span class="cell-main">${deltaAmt >= 0 ? '+' : ''}${formatMoney(deltaAmt)}</span>
          <span class="cell-delta">${formatPct(deltaPct, true)}</span>
        </td>
        <td class="${getReturnClass(totalRet)}">${formatPct(totalRet, true)}</td>
        <td><a href="#" onclick="deleteEntry(${i});return false;" style="color:#e74c3c;text-decoration:none;font-size:12px;">删除</a></td>
      </tr>
    `;
  }).join('');
}

function capitalize(str) {
  return str.charAt(0).toUpperCase() + str.slice(1);
}
