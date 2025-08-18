const express = require('express');
const app = express();
app.use(express.json());
const PORT = 6002;

// GET /api/v1/user/exists?userId=xxx
app.get('/api/v1/user/exists', (req, res) => {
  // userId 파라미터 사용
  res.json({ authType: 'email' });
});

// POST /api/v1/auth/
app.post('/api/v1/auth/', (req, res) => {
  // Authorization 헤더 필요
  // 실제 인증 검증은 생략, 항상 email 반환
  res.json({ authType: 'email' });
});

// GET /api/v1/user/profile
app.get('/api/v1/user/profile', (req, res) => {
  // Authorization 헤더 필요
  // 예시 응답
  res.json({ userId: 'brightdelusion@gmail.com', regionCode: '1156058500' });
});

// GET /api/v1/device/user-devices
app.get('/api/v1/device/user-devices', (req, res) => {
  // Authorization 헤더 필요
  // 예시 디바이스 정보 배열
  res.json([
    {
      mac: 511832,
      macString: '07CF58',
      name: 'MH',
      location: '거실',
      regionCode: '1156058500',
      deviceType: 'purethink',
      freeFilterCycle: 3000,
      hepaFilterCycle: 6000,
      fwVersion: 'ver.210826.1530_DIV0'
    }
  ]);
});

// GET /api/v1/atmospheric/data?regionCode=xxx
app.get('/api/v1/atmospheric/data', (req, res) => {
  // Authorization 헤더 필요
  // regionCode 파라미터 사용
  res.json({
    regionCode: req.query.regionCode || '1156058500',
    weather: '비',
    temperature: 28,
    regionName: '서울',
    city: '영등포구',
    dongEubMyeon: '도림동',
    Ozone: 0.025,
    Pm10: 28,
    Pm25: 11
  });
});

app.listen(PORT, () => {
  console.log(`Server listening on port ${PORT}`);
});
