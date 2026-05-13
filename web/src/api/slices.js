import api from './client'

export const getSlices = () =>
  api.get('/slices').then(r => r.data)

export const getSlice = (slice_uid) =>
  api.get(`/slices/${slice_uid}`).then(r => r.data)

export const createSlice = (data) =>
  api.post('/slices', data).then(r => r.data)

export const deleteSlice = (slice_uid) =>
  api.delete(`/slices/${slice_uid}`).then(r => r.data)

export const getJob = (job_uid) =>
  api.get(`/jobs/${job_uid}`).then(r => r.data)

export const getServers = () =>
  api.get('/servers').then(r => r.data)