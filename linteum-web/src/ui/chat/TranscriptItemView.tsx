import type { TranscriptItem } from "../../state/types";
import { AssistantItemView } from "./items/AssistantItem";
import { CompactionItemView } from "./items/CompactionItem";
import { ErrorItemView } from "./items/ErrorItem";
import { NoticeItemView } from "./items/NoticeItem";
import { SubagentItemView } from "./items/SubagentItem";
import { ToolItemView } from "./items/ToolItem";
import { UserItemView } from "./items/UserItem";

export function TranscriptItemView({ item }: { item: TranscriptItem }) {
  switch (item.kind) {
    case "user":
      return <UserItemView item={item} />;
    case "assistant":
      return <AssistantItemView item={item} />;
    case "tool":
      return <ToolItemView item={item} />;
    case "subagent":
      return <SubagentItemView item={item} />;
    case "compaction":
      return <CompactionItemView item={item} />;
    case "notice":
      return <NoticeItemView item={item} />;
    case "error":
      return <ErrorItemView item={item} />;
    default:
      return null;
  }
}
