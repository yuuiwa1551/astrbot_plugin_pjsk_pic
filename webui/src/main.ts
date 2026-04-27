import { createApp } from 'vue';
import App from './App.vue';
import { router, normalizeEntryUrl } from './router';
import './styles.css';

normalizeEntryUrl();

createApp(App).use(router).mount('#app');
