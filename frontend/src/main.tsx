// React 页面入口，主窗口与悬浮窗共用 App，具体界面通过 location.hash 判断。
// StrictMode 在开发时帮助检查副作用清理，所以订阅和定时器都必须在卸载时释放。
import React from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import './style.css';
createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
