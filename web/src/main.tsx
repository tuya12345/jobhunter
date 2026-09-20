import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import App from './App.tsx';
import './styles.css';

const container = document.getElementById('root');
if (container === null) {
  throw new Error('index.html 里找不到 #root，页面没法挂载');
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
