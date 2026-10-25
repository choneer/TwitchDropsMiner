const {test} = require('node:test');
const assert = require('node:assert/strict');
const view = require('../webui/static/campaigns.js');
test('finished inventory never masquerades as unfinished open activity', () => {
  const c = {name:'Anniversary', game:'Warhammer', active:true, finished:true, in_inventory:true, eligible:true, earnable:false};
  assert.equal(view.status(c), '已全部领取');
  assert.equal(view.matches(c, 'open', ''), false);
  assert.equal(view.matches(c, 'eligible', ''), false);
  assert.equal(view.matches(c, 'inventory', ''), true);
  assert.equal(view.matches(c, 'finished', ''), true);
});
test('public activity is discoverable without pretending account eligibility', () => {
  const c = {name:'Event',game:'PUBG: BATTLEGROUNDS',active:true,finished:false,eligible:false,earnable:true};
  assert.equal(view.matches(c, 'open', 'pubg'), true);
  assert.equal(view.matches(c, 'eligible', 'pubg'), false);
  assert.equal(view.matches(c, 'open', 'other'), false);
  assert.equal(view.status(c), '已开放');
});
test('empty search, missing authorization and discovery failure have distinct messages', () => {
  assert.match(view.empty(false, null, false), /授权/);
  assert.match(view.empty(true, null, true), /筛选/);
  assert.equal(view.empty(true, {status:'error',message:'目录暂不可用'}, true), '目录暂不可用');
});
