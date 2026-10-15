/* Wan's AI Business OS v0.1 —— 极简前端脚本
 * 只做一件事：带有 data-confirm 属性的表单，提交前弹出二次确认。
 */
(function () {
  'use strict';

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form || !form.getAttribute) return;
    var msg = form.getAttribute('data-confirm');
    if (msg && !window.confirm(msg)) {
      e.preventDefault();
    }
  }, true);
})();
