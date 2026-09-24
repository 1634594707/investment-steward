/** 演示模式明示横幅（路线图 A1）：页面内容为界面示例，刷新即还原、不写入本机数据库。 */
export function DemoNotice({ context }: { context?: string }) {
  return (
    <div className="demo-notice" role="note">
      <strong>演示模式</strong>
      <span>
        本页{context ? `（${context}）` : ""}展示的是界面示例数据，仅用于走查界面结构；刷新或重启即还原，不会写入本机数据库。
        启动桌面端（Host 桥）后自动切换为真实 Core 数据。
      </span>
    </div>
  );
}
