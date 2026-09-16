import { createRoot } from 'react-dom/client';
import App from './App';
import './styles.css';

document.documentElement.dataset.runtime = '__TAURI_INTERNALS__' in window ? 'native' : 'browser';

createRoot(document.getElementById('root')!).render(<App />);
