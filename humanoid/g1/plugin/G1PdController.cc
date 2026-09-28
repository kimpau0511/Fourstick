// G1 다리 관절 PD — 물리 한 스텝마다 τ = kp(q* − q) − kd·q̇ (토크 한계로 자름).
//
// 학습·MuJoCo 배치와 같은 식을 **Gazebo 안에서** 500 Hz로 돈다. 파이썬 정책은 50 Hz로
// 목표 자세 q*만 보낸다(`<target_topic>`, gz.msgs.Double_V = [seq, q*0..q*11]).
// 받은 목표를 저장한 **뒤** `<ack_topic>`(gz.msgs.Int32 = seq)로 알린다 — 제어기는 이 알림을
// 받고서 스텝을 민다(목표와 스텝 요청의 순서 보장).
// 토크를 끄는 명령은 없다. 목표가 오기 전에는 `<default>` 자세를 목표로 쓴다.
#include <gz/msgs/double_v.pb.h>
#include <gz/msgs/int32.pb.h>

#include <algorithm>
#include <mutex>
#include <sstream>
#include <string>
#include <vector>

#include <gz/common/Console.hh>
#include <gz/plugin/Register.hh>
#include <gz/sim/Joint.hh>
#include <gz/sim/Model.hh>
#include <gz/sim/System.hh>
#include <gz/transport/Node.hh>

namespace forstick2
{
static std::vector<double> ParseList(const sdf::ElementPtr &_sdf, const std::string &_name)
{
  std::vector<double> out;
  if (!_sdf->HasElement(_name))
    return out;
  std::istringstream in(_sdf->Get<std::string>(_name));
  double v;
  while (in >> v)
    out.push_back(v);
  return out;
}

class G1PdController : public gz::sim::System,
                       public gz::sim::ISystemConfigure,
                       public gz::sim::ISystemPreUpdate
{
public:
  void Configure(const gz::sim::Entity &_entity,
                 const std::shared_ptr<const sdf::Element> &_sdf,
                 gz::sim::EntityComponentManager &_ecm,
                 gz::sim::EventManager &) override
  {
    auto sdf = _sdf->Clone();
    this->model = gz::sim::Model(_entity);
    std::istringstream names(sdf->Get<std::string>("joints"));
    std::string name;
    while (names >> name)
      this->jointNames.push_back(name);
    this->kp = ParseList(sdf, "kp");
    this->kd = ParseList(sdf, "kd");
    this->effort = ParseList(sdf, "effort");
    this->target = ParseList(sdf, "default");
    const size_t n = this->jointNames.size();
    if (n == 0 || this->kp.size() != n || this->kd.size() != n ||
        this->effort.size() != n || this->target.size() != n)
    {
      gzerr << "[G1PdController] joints/kp/kd/effort/default 길이가 맞지 않는다\n";
      this->jointNames.clear();
      return;
    }
    const auto targetTopic = sdf->Get<std::string>("target_topic", "/g1/pd/target").first;
    const auto ackTopic = sdf->Get<std::string>("ack_topic", "/g1/pd/ack").first;
    this->ackPub = this->node.Advertise<gz::msgs::Int32>(ackTopic);
    this->node.Subscribe(targetTopic, &G1PdController::OnTarget, this);
    gzmsg << "[G1PdController] " << n << " joints, target " << targetTopic
          << ", ack " << ackTopic << "\n";
    (void)_ecm;
  }

  void PreUpdate(const gz::sim::UpdateInfo &, gz::sim::EntityComponentManager &_ecm) override
  {
    if (this->jointNames.empty())
      return;
    if (this->joints.empty())
    {
      for (const auto &name : this->jointNames)
      {
        auto entity = this->model.JointByName(_ecm, name);
        if (entity == gz::sim::kNullEntity)
        {
          gzerr << "[G1PdController] 관절을 찾지 못했다: " << name << "\n";
          this->jointNames.clear();
          return;
        }
        gz::sim::Joint joint(entity);
        joint.EnablePositionCheck(_ecm, true);
        joint.EnableVelocityCheck(_ecm, true);
        this->joints.push_back(joint);
      }
    }
    std::vector<double> goal;
    {
      std::lock_guard<std::mutex> lock(this->mutex);
      goal = this->target;
    }
    for (size_t i = 0; i < this->joints.size(); ++i)
    {
      auto pos = this->joints[i].Position(_ecm);
      auto vel = this->joints[i].Velocity(_ecm);
      if (!pos || !vel || pos->empty() || vel->empty())
        continue;  // 첫 스텝은 상태가 아직 없다
      double tau = this->kp[i] * (goal[i] - pos->front()) - this->kd[i] * vel->front();
      tau = std::clamp(tau, -this->effort[i], this->effort[i]);
      this->joints[i].SetForce(_ecm, {tau});
    }
  }

private:
  void OnTarget(const gz::msgs::Double_V &_msg)
  {
    const size_t n = this->jointNames.size();
    if (static_cast<size_t>(_msg.data_size()) != n + 1)
      return;
    {
      std::lock_guard<std::mutex> lock(this->mutex);
      for (size_t i = 0; i < n; ++i)
        this->target[i] = _msg.data(static_cast<int>(i) + 1);
    }
    gz::msgs::Int32 ack;
    ack.set_data(static_cast<int>(_msg.data(0)));
    this->ackPub.Publish(ack);
  }

  gz::sim::Model model{gz::sim::kNullEntity};
  std::vector<std::string> jointNames;
  std::vector<gz::sim::Joint> joints;
  std::vector<double> kp, kd, effort, target;
  std::mutex mutex;
  gz::transport::Node node;
  gz::transport::Node::Publisher ackPub;
};
}  // namespace forstick2

GZ_ADD_PLUGIN(forstick2::G1PdController, gz::sim::System,
              forstick2::G1PdController::ISystemConfigure,
              forstick2::G1PdController::ISystemPreUpdate)
