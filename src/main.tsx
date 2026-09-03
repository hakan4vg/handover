import { createRoot } from 'react-dom/client';
import App from './App';
import './styles.css';

document.documentElement.dataset.runtime = Boolean((window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__) ? 'native' : 'browser';

createRoot(document.getElementById('root')!).render(<App />);
