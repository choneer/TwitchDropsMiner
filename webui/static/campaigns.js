/* Shared presentation rules, also exercised by the offline Node tests. */
"use strict";
const CampaignView = {
  status(c) {
    if (c.finished) return "已全部领取";
    if (c.expired) return "已结束";
    if (c.upcoming) return "即将开始";
    return "已开放";
  },
  matches(c, filter, query) {
    if (!(c.name + " " + c.game).toLowerCase().includes(query.toLowerCase())) return false;
    switch (filter) {
      case "open": return c.active && !c.finished;
      case "eligible": return c.eligible && c.earnable && !c.finished;
      case "finished": return c.finished;
      case "inventory": return c.in_inventory;
      case "upcoming": return c.upcoming && !c.finished;
      case "expired": return c.expired;
      default: return true;
    }
  },
  empty(authenticated, discovery, hasCampaigns) {
    if (!authenticated) return "请先启动服务并完成 Twitch 授权。";
    if (discovery?.status === "error") return discovery.message;
    return hasCampaigns ? "没有符合当前搜索或筛选条件的活动，可清空搜索或切换到全部活动。"
      : "尚未取得活动数据，请查看运行状态或刷新活动。";
  },
};
if (typeof module !== "undefined") module.exports = CampaignView;
