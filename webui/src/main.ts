import { createApp } from 'vue';
import App from './App.vue';
import { router, normalizeLegacyHash } from './router';
import './styles.css';

normalizeLegacyHash();

createApp(App).use(router).mount('#app');
