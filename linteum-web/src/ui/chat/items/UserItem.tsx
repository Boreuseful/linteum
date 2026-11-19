import type { UserItem as UserItemModel } from "../../../state/types";

export function UserItemView({ item }: { item: UserItemModel }) {
  return (
    <div className="msg msg-user">
      <div className="bubble">{item.text}</div>
    </div>
  );
}
