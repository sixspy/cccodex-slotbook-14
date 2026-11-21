export const reconcileViewTemplate = `
  <div v-show="tab===4" class="panel">
    <h2>对账互证</h2>
    <p :class="recon.all_ok ? 'ok' : 'err'">{{ recon.all_ok ? '全部同源' : '存在分裂：盘面 / 正式册 / 互证册不一致' }}</p>
    <table>
      <thead><tr><th>档</th><th>盘面已订</th><th>正式册席</th><th>互证册席</th><th>结果</th></tr></thead>
      <tbody>
        <tr v-for="i in (recon.items||[])" :key="i.slot_code">
          <td>{{ i.slot_code }}</td><td>{{ i.slot_booked }}</td>
          <td>{{ i.booking_seats }}</td><td>{{ i.attestation_seats }}</td>
          <td :class="i.ok?'ok':'err'">{{ i.ok ? '通过' : '分裂' }}</td>
        </tr>
      </tbody>
    </table>
    <h3>当事人互证</h3>
    <div>
      <input v-model="proofParty" placeholder="当事人 party_key，如 P-AT1" style="width:14rem" />
      <button @click="loadProof">查互证</button>
    </div>
    <p :class="(proof && proof.attestation_ok) ? 'ok' : (proof ? 'err' : '')">
      <template v-if="proof">
        {{ proof.attestation_ok
          ? '互证同源：已确认 ' + proof.confirmed_count + ' / 已签发 ' + proof.attested_count
          : '互证缺失：已确认 ' + proof.confirmed_count + ' / 已签发 ' + proof.attested_count + '，请按确认互证说明排查' }}
      </template>
    </p>
    <table v-if="proof">
      <thead><tr><th>互证号</th><th>正式册 ID</th><th>当事人</th><th>席</th><th>快照代</th><th>态</th></tr></thead>
      <tbody>
        <tr v-for="a in proof.attestations" :key="a.id">
          <td>{{ a.proof_code }}</td><td>{{ a.booking_id }}</td><td>{{ a.party_key }}</td>
          <td>{{ a.seats }}</td><td>{{ a.snapshot_gen }}</td><td>{{ a.status }}</td>
        </tr>
        <tr v-if="!proof.attestations.length"><td colspan="6" class="err">互证页空：该当事人无已签发互证</td></tr>
      </tbody>
    </table>
  </div>
`;
