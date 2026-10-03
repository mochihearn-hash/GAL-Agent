// 欢迎页只保留创建入口；看板娘是本地装饰资源，不传给模型，也不参与游戏截图。
import { Plus } from 'lucide-react';

// Vite 会将图片写入构建资源并改写地址，源码运行和 Electron 打包均可离线加载。
const mascot = new URL('./assets/claude-mascot.png', import.meta.url).href;

export function Welcome({ onCreate }: { onCreate: () => void }) {
  return <section className="welcome welcome-mascot" aria-label="欢迎页">
    <img className="welcome-mascot-image" src={mascot} alt="" aria-hidden="true" draggable={false}/>
    <div className="welcome-mascot-wash" aria-hidden="true"/>
    <button className="primary large welcome-create" onClick={onCreate}>
      <Plus size={18}/>创建第一个会话
    </button>
  </section>;
}
