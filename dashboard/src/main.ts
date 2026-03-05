import { createApp } from 'vue'
import { createPinia } from 'pinia'
import vuetify from '@/plugins/vuetify'
import router from '@/router'
import App from '@/App.vue'

import '@mdi/font/css/materialdesignicons.css'

createApp(App)
  .use(vuetify)
  .use(router)
  .use(createPinia())
  .mount('#app')
